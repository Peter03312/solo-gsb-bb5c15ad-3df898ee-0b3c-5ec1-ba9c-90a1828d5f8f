"""车次、装卸时段与车载测温读数的写入 / 查询。

加锁顺序（全系统统一，避免死锁）
--------------------------------
**先按 cargo_id 升序锁定受影响货物行，再锁定车次行**；自有事件写入与责任表态
只锁单个货物行。因此：

* 车载读数写入与“受影响货物”的事件写入、责任表态严格串行——读数持锁期间，
  受影响货物的越温时间线不会被别的事务改动；
* 装卸登记与车载读数写入对同一车次/货物也经货物行锁 + 车次行锁串行。

装卸时段登记（manifest）为批量原子操作：任一货物不存在、任一时段非法
（起点不早于终点）或与该货物既有/同批时段重叠（跨车次重叠即“同货物同时
属于两个车次”），整批拒绝，不留部分记录。

车载读数允许晚到；reading_id 同内容重传幂等、异内容冲突；同车次同刻至多
一笔读数。读数本身不改变任何货物的保管方（交接仍只由货物自有事件产生）。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, OperationalError

from app import chain, coexcursion, timeline
from app.crud import ApiError
from app.models import Cargo, Loading, Trip, VehicleReading
from app.schemas import ManifestCreate, TripCreate, VehicleReadingCreate
from app.timeutil import ensure_utc


def _conflict(message: str, code: str) -> ApiError:
    return ApiError(409, code, message)


def _unprocessable(message: str, code: str) -> ApiError:
    return ApiError(422, code, message)


def _not_found(message: str) -> ApiError:
    return ApiError(404, "trip_not_found", message)


# ----------------------------------------------------------- 车次


def create_trip(db, payload: TripCreate) -> Trip:
    trip = Trip(
        trip_id=payload.trip_id,
        vehicle_no=payload.vehicle_no,
        note=payload.note,
    )
    db.add(trip)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise _conflict(f"车次编号 {payload.trip_id} 已存在", code="trip_exists")
    return trip


def list_trips(db) -> list[Trip]:
    return list(db.scalars(select(Trip).order_by(Trip.trip_id)).all())


def get_trip(db, trip_id: str) -> Trip:
    trip = db.get(Trip, trip_id)
    if trip is None:
        raise _not_found(f"车次 {trip_id} 不存在")
    return trip


def _load_trip_locked(db, trip_id: str) -> Trip:
    trip = db.scalars(
        select(Trip).where(Trip.trip_id == trip_id).with_for_update()
    ).first()
    if trip is None:
        raise ApiError(404, "trip_not_found", f"车次 {trip_id} 不存在")
    return trip


def _lock_cargo_rows(db, cargo_ids):
    """按 cargo_id 升序锁定货物行；任一不存在返回 404。"""
    rows = {}
    missing = []
    for cargo_id in sorted(set(cargo_ids)):
        cargo = db.scalars(
            select(Cargo).where(Cargo.cargo_id == cargo_id).with_for_update()
        ).first()
        if cargo is None:
            missing.append(cargo_id)
        else:
            rows[cargo_id] = cargo
    if missing:
        raise ApiError(
            404,
            "cargo_not_found",
            f"货物 {', '.join(sorted(missing))} 不存在",
        )
    return rows


def _windows_overlap(lo1, un1, lo2, un2) -> bool:
    """两个左闭右开时段是否重叠（首尾相接不算重叠）。"""
    return lo1 < un2 and lo2 < un1


# ----------------------------------------------------------- 装卸登记


@dataclass
class ManifestResult:
    trip_id: str
    items: list[Loading]


def register_manifest(db, payload: ManifestCreate):
    try:
        return _register_manifest_tx(db, payload)
    except OperationalError:
        # SQLite 文件库锁等待耗尽 / PG 死锁检测中止：整事务重放，语义不变
        db.rollback()
        return _register_manifest_tx(db, payload)


def _register_manifest_tx(db, payload: ManifestCreate) -> ManifestResult:
    # 1) 时段形状：左闭右开且起点严格早于终点（先做无锁校验，整批不落库）
    norm = []
    for it in payload.items:
        loaded_at = ensure_utc(it.loaded_at)
        unloaded_at = ensure_utc(it.unloaded_at)
        if loaded_at >= unloaded_at:
            raise _unprocessable(
                f"车次 {payload.trip_id} 货物 {it.cargo_id} 的装卸时段 "
                f"[{loaded_at.isoformat()}, {unloaded_at.isoformat()}) 无效："
                "装载时段必须左闭右开且起点严格早于终点",
                code="invalid_loading_window",
            )
        norm.append((it.cargo_id, loaded_at, unloaded_at))

    try:
        # 2) 车次须存在（先读；真正互斥在拿到车次行锁后）
        if db.get(Trip, payload.trip_id) is None:
            raise ApiError(
                404, "trip_not_found", f"车次 {payload.trip_id} 不存在"
            )

        # 3) 先按 cargo_id 升序锁定本批涉及的全部货物行
        _lock_cargo_rows(db, [c for c, _, _ in norm])

        # 4) 再锁定车次行
        _load_trip_locked(db, payload.trip_id)

        # 5) 同批内同一货物时段不得互相重叠
        by_cargo: dict[str, list] = {}
        for cargo_id, lo, un in norm:
            for lo2, un2 in by_cargo.setdefault(cargo_id, []):
                if _windows_overlap(lo, un, lo2, un2):
                    raise _conflict(
                        f"货物 {cargo_id} 在本批登记中存在重叠装卸时段 "
                        f"[{lo.isoformat()}, {un.isoformat()}) 与 "
                        f"[{lo2.isoformat()}, {un2.isoformat()})",
                        code="loading_overlap",
                    )
            by_cargo[cargo_id].append((lo, un))

        # 6) 与该货物既有装卸时段（任意车次）比对：跨车次重叠即同货物同时属于两个车次
        existing = list(
            db.scalars(
                select(Loading).where(
                    Loading.cargo_id.in_(sorted({c for c, _, _ in norm}))
                )
            ).all()
        )
        for l in existing:
            l.loaded_at = ensure_utc(l.loaded_at)
            l.unloaded_at = ensure_utc(l.unloaded_at)
        for cargo_id, lo, un in norm:
            for l in existing:
                if l.cargo_id != cargo_id:
                    continue
                if _windows_overlap(lo, un, l.loaded_at, l.unloaded_at):
                    raise _conflict(
                        f"货物 {cargo_id} 的装卸时段 "
                        f"[{lo.isoformat()}, {un.isoformat()}) 与车次 {l.trip_id} "
                        f"既有时段 [{l.loaded_at.isoformat()}, "
                        f"{l.unloaded_at.isoformat()}) 重叠；"
                        "同一货物不得同时属于两个车次",
                        code="loading_overlap",
                    )

        rows = [
            Loading(
                trip_id=payload.trip_id,
                cargo_id=cargo_id,
                loaded_at=lo,
                unloaded_at=un,
            )
            for cargo_id, lo, un in norm
        ]
        db.add_all(rows)
        db.commit()
    except ApiError:
        db.rollback()
        raise
    except IntegrityError:
        db.rollback()
        raise _conflict(
            f"车次 {payload.trip_id} 的装卸登记与既有数据冲突"
            "（重复时段 / 车次或货物缺失）",
            code="integrity_conflict",
        )

    for l in rows:
        l.loaded_at = ensure_utc(l.loaded_at)
        l.unloaded_at = ensure_utc(l.unloaded_at)
    return ManifestResult(trip_id=payload.trip_id, items=rows)


# ----------------------------------------------------------- 车载读数


@dataclass
class ReadingResult:
    reading: VehicleReading
    replayed: bool
    affected_cargo_ids: list[str]


def create_vehicle_reading(db, payload: VehicleReadingCreate) -> ReadingResult:
    try:
        return _create_reading_tx(db, payload)
    except OperationalError:
        # SQLite 文件库锁等待耗尽 / PG 死锁检测中止：整事务重放，语义不变
        db.rollback()
        return _create_reading_tx(db, payload)


def _same_reading_content(existing: VehicleReading, payload, read_at) -> bool:
    return (
        existing.trip_id == payload.trip_id
        and ensure_utc(existing.read_at) == read_at
        and existing.temperature == payload.temperature
    )


def _create_reading_tx(db, payload: VehicleReadingCreate) -> ReadingResult:
    read_at = ensure_utc(payload.read_at)
    try:
        # 车次须存在
        if db.get(Trip, payload.trip_id) is None:
            raise ApiError(
                404, "trip_not_found", f"车次 {payload.trip_id} 不存在"
            )

        # 幂等先判定（按主键读，无需持锁）
        existing = db.get(VehicleReading, payload.reading_id)
        if existing is not None:
            if _same_reading_content(existing, payload, read_at):
                db.rollback()
                affected = _affected_cargo_ids(db, payload.trip_id, read_at)
                return ReadingResult(existing, True, affected)
            raise _conflict(
                f"车载读数编号 {payload.reading_id} 已存在且内容不同，拒绝覆盖",
                code="reading_id_conflict",
            )

        # 预估受影响货物（此刻在车上的货物）
        affected = _affected_cargo_ids(db, payload.trip_id, read_at)

        # 1) 先锁定全部受影响货物行（升序），与这些货物的事件/表态写入串行
        _lock_cargo_rows(db, affected)
        # 2) 再锁定车次行，与装卸登记、同车次读数写入串行
        _load_trip_locked(db, payload.trip_id)

        # 拿到车次锁后复核幂等（并发重传）
        winner = db.get(VehicleReading, payload.reading_id)
        if winner is not None:
            if _same_reading_content(winner, payload, read_at):
                db.rollback()
                affected2 = _affected_cargo_ids(db, payload.trip_id, read_at)
                return ReadingResult(winner, True, affected2)
            raise _conflict(
                f"车载读数编号 {payload.reading_id} 已存在且内容不同，拒绝覆盖",
                code="reading_id_conflict",
            )

        # 同车次同刻至多一笔读数
        same_slot = db.scalars(
            select(VehicleReading).where(
                VehicleReading.trip_id == payload.trip_id,
                VehicleReading.read_at == read_at,
            )
        ).first()
        if same_slot is not None:
            raise _conflict(
                f"车次 {payload.trip_id} 在 {read_at.isoformat()} "
                f"已存在车载读数 {same_slot.reading_id}，同一时刻不允许重复",
                code="same_timestamp_conflict",
            )

        reading = VehicleReading(
            reading_id=payload.reading_id,
            trip_id=payload.trip_id,
            read_at=read_at,
            temperature=payload.temperature,
        )
        db.add(reading)
        db.commit()
    except ApiError:
        db.rollback()
        raise
    except IntegrityError:
        db.rollback()
        winner = db.get(VehicleReading, payload.reading_id)
        if winner is not None:
            if _same_reading_content(winner, payload, read_at):
                affected2 = _affected_cargo_ids(db, payload.trip_id, read_at)
                return ReadingResult(winner, True, affected2)
            raise _conflict(
                f"车载读数编号 {payload.reading_id} 已存在且内容不同，拒绝覆盖",
                code="reading_id_conflict",
            )
        raise _conflict(
            f"车载读数 {payload.reading_id} 与既有数据冲突"
            "（同车次同刻重复 / 车次缺失 / 字段约束）",
            code="integrity_conflict",
        )

    reading.read_at = ensure_utc(reading.read_at)
    return ReadingResult(reading, False, affected)


def _affected_cargo_ids(db, trip_id: str, read_at) -> list[str]:
    """读数时刻仍在该车次车上的货物（左闭右开）。"""
    rows = db.scalars(
        select(Loading.cargo_id).where(
            Loading.trip_id == trip_id,
            Loading.loaded_at <= read_at,
            Loading.unloaded_at > read_at,
        )
    ).all()
    return sorted(set(rows))


# ----------------------------------------------------------- 查询


def get_loadings(db, trip_id: str) -> list[Loading]:
    return list(
        db.scalars(
            select(Loading)
            .where(Loading.trip_id == trip_id)
            .order_by(Loading.loaded_at, Loading.cargo_id)
        ).all()
    )


def get_readings(db, trip_id: str) -> list[VehicleReading]:
    return list(
        db.scalars(
            select(VehicleReading)
            .where(VehicleReading.trip_id == trip_id)
            .order_by(VehicleReading.read_at, VehicleReading.reading_id)
        ).all()
    )


# ----------------------------------------------------------- 同时越温查询


def _begin_snapshot_read(db) -> None:
    """让本次查询的全部 SELECT 基于同一份已提交数据视图。

    PostgreSQL 默认 READ COMMITTED 下每条语句各自取快照，并发的事件 / 车载读数
    写入可能使同次查询内读到的装卸、事件、读数不属于同一时刻；在事务首个语句
    位置提升为 REPEATABLE READ 后，整个查询看到事务开始时已提交的一致快照。
    SQLite 单连接事务内的读天然一致，无需处理。
    """
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))


def find_co_excursions(
    db,
    trip_id: str,
    query_start,
    query_end,
    min_cargo_count: int,
) -> list[coexcursion.CoSegment]:
    """查找车次上同时越温（>= min_cargo_count 票）的时段。

    对车次上每票货物：在合并读数时间线上重建越温区间（同刻自有测温优先），
    与其在本车次的装卸时段（左闭右开）及查询窗相交，未闭合区间裁剪至查询
    终点；再做扫描线合并，仅保留达到门槛的正长度片段。
    """
    if min_cargo_count < 2:
        raise _unprocessable(
            f"数量门槛 min_cargo_count={min_cargo_count} 非法："
            "同时越温查询的门槛不得小于 2 票",
            code="invalid_min_cargo_count",
        )
    query_start = ensure_utc(query_start)
    query_end = ensure_utc(query_end)
    if query_end <= query_start:
        raise _unprocessable(
            f"查询窗无效：终点 {query_end.isoformat()} 不晚于起点 "
            f"{query_start.isoformat()}",
            code="invalid_query_window",
        )

    # 在任何 SELECT 之前提升隔离级别：同次查询基于一致的已提交数据视图
    _begin_snapshot_read(db)

    if db.get(Trip, trip_id) is None:
        raise _not_found(f"车次 {trip_id} 不存在")

    loadings = get_loadings(db, trip_id)
    for l in loadings:
        l.loaded_at = ensure_utc(l.loaded_at)
        l.unloaded_at = ensure_utc(l.unloaded_at)
    trip_loadings: dict[str, list[Loading]] = {}
    for l in loadings:
        trip_loadings.setdefault(l.cargo_id, []).append(l)

    cargos = {
        c.cargo_id: c
        for c in db.scalars(
            select(Cargo).where(
                Cargo.cargo_id.in_(sorted(trip_loadings) or [""])
            )
        ).all()
    }

    per_cargo: dict[str, list] = {}
    for cargo_id in sorted(trip_loadings):
        cargo = cargos[cargo_id]
        cargo.started_at = ensure_utc(cargo.started_at)
        events, all_loadings, readings = timeline.load_cargo_context(
            db, cargo_id
        )
        points = timeline.adopted_timeline(events, readings, all_loadings)
        result = chain.rebuild_chain(
            cargo.initial_holder, Decimal(cargo.temp_upper_limit), points
        )
        pieces = coexcursion.clip_cargo_excursions(
            result.excursions,
            trip_loadings[cargo_id],
            query_start,
            query_end,
        )
        if pieces:
            per_cargo[cargo_id] = pieces

    return coexcursion.simultaneous_segments(per_cargo, min_cargo_count)
