"""车次同时越温时段查询（GET /trips/{trip_id}/co-excursions）集成测试。

覆盖：
* 基本场景：多票货物按各自上限越温，集合变化即分段，仅保留达到门槛的片段；
* 数量门槛：默认值、过滤效果、非法门槛（<2）拒绝；
* 查询窗：终点不晚于起点拒绝、查询窗裁剪、带时区偏移的起止时刻；
* 未闭合区间裁剪至查询终点；越温区间裁剪至本车次装卸时段（左闭右开）；
* 同刻自有测温优先影响同时越温结果；
* 分段承运（同车次多段首尾相接装卸）时相邻同集合片段合并；
* 晚到读数写入后查询即反映新结果；不在本车次的货物不计入；
* 未知车次 404、无匹配返回空结果。
"""
from datetime import datetime, timezone

from app import coexcursion


def post_trip(client, trip_id="T1", vehicle_no="京A-001"):
    return client.post(
        "/trips", json={"trip_id": trip_id, "vehicle_no": vehicle_no}
    )


def post_manifest(client, trip_id, items):
    return client.post(f"/trips/{trip_id}/manifest", json={"items": items})


def post_reading(client, rid, trip_id, at, temp):
    return client.post(
        "/vehicle-readings",
        json={
            "reading_id": rid,
            "trip_id": trip_id,
            "read_at": at,
            "temperature": str(temp),
        },
    )


def m(client, eid, at, temp, cargo):
    return client.post(
        "/events",
        json={"event_id": eid, "cargo_id": cargo,
              "occurred_at": at, "temperature": str(temp)},
    )


def make_cargo(client, cargo_id, limit="5"):
    r = client.post("/cargo", json={
        "cargo_id": cargo_id,
        "started_at": "2026-01-01T08:00:00+00:00",
        "temp_upper_limit": limit,
        "initial_holder": "A",
    })
    assert r.status_code == 201, r.text


def load_10_20(client, trip_id, cargo_id):
    return {
        "cargo_id": cargo_id,
        "loaded_at": "2026-01-01T10:00:00+00:00",
        "unloaded_at": "2026-01-01T20:00:00+00:00",
    }


def co_query(client, trip_id, start, end, min_count=None):
    params = {"start": start, "end": end}
    if min_count is not None:
        params["min_cargo_count"] = min_count
    return client.get(f"/trips/{trip_id}/co-excursions", params=params)


def setup_two_cargo_staggered(client):
    """C1（上限5）越温 [11,16)，C2（上限8）越温 [12,14)。"""
    make_cargo(client, "C1", "5")
    make_cargo(client, "C2", "8")
    post_trip(client)
    post_manifest(client, "T1", [load_10_20(client, "T1", "C1"),
                                 load_10_20(client, "T1", "C2")])
    post_reading(client, "r1", "T1", "2026-01-01T11:00:00+00:00", 7)   # C1 越温
    post_reading(client, "r2", "T1", "2026-01-01T12:00:00+00:00", 9)   # 都越温
    post_reading(client, "r3", "T1", "2026-01-01T14:00:00+00:00", 7)   # C2 闭合
    post_reading(client, "r4", "T1", "2026-01-01T16:00:00+00:00", 3)   # C1 闭合


# ------------------------------------------------------------- 基本查询


def test_co_excursion_basic_set_change_segmentation(client):
    setup_two_cargo_staggered(client)
    r = co_query(client, "T1",
                 "2026-01-01T09:00:00+00:00", "2026-01-01T21:00:00+00:00")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["trip_id"] == "T1"
    assert body["query_start"] == "2026-01-01T09:00:00Z"
    assert body["query_end"] == "2026-01-01T21:00:00Z"
    assert body["min_cargo_count"] == 2  # 默认门槛 2
    # 仅 [12,14) 同时两票越温；[11,12) 与 [14,16) 只有 C1 一票，不达门槛
    assert body["segments"] == [
        {"start": "2026-01-01T12:00:00Z", "end": "2026-01-01T14:00:00Z",
         "cargo_ids": ["C1", "C2"], "cargo_count": 2}
    ]


def test_co_excursion_threshold_filters_and_default(client):
    setup_two_cargo_staggered(client)
    # 门槛 3：两车同时段也只有 2 票 -> 空
    r = co_query(client, "T1",
                 "2026-01-01T09:00:00+00:00", "2026-01-01T21:00:00+00:00",
                 min_count=3)
    assert r.status_code == 200
    assert r.json()["segments"] == []


def test_co_excursion_multi_segment_sorted_non_overlapping(client):
    # 三票同车：C3 在 12:00 由自有测温闭合，C1/C2 到 14:00 才闭合
    for cid in ("C1", "C2", "C3"):
        make_cargo(client, cid, "5")
    post_trip(client)
    post_manifest(client, "T1", [load_10_20(client, "T1", cid)
                                 for cid in ("C1", "C2", "C3")])
    post_reading(client, "r1", "T1", "2026-01-01T10:00:00+00:00", 9)  # 三票越温
    m(client, "m-c3", "2026-01-01T12:00:00+00:00", 3, "C3")           # C3 闭合
    post_reading(client, "r2", "T1", "2026-01-01T14:00:00+00:00", 3)  # C1/C2 闭合

    body = co_query(client, "T1",
                    "2026-01-01T09:00:00+00:00",
                    "2026-01-01T21:00:00+00:00").json()
    segs = body["segments"]
    assert [(s["start"], s["end"], s["cargo_count"]) for s in segs] == [
        ("2026-01-01T10:00:00Z", "2026-01-01T12:00:00Z", 3),
        ("2026-01-01T12:00:00Z", "2026-01-01T14:00:00Z", 2),
    ]
    assert segs[0]["cargo_ids"] == ["C1", "C2", "C3"]
    assert segs[1]["cargo_ids"] == ["C1", "C2"]
    # 按时间升序且互不重叠（半开区间首尾相接）
    for prev, nxt in zip(segs, segs[1:]):
        assert prev["end"] <= nxt["start"]


def test_co_excursion_empty_when_no_simultaneous(client):
    make_cargo(client, "C1", "5")
    make_cargo(client, "C2", "5")
    post_trip(client)
    post_manifest(client, "T1", [load_10_20(client, "T1", "C1"),
                                 load_10_20(client, "T1", "C2")])
    # C1 越温 [11,12)；12:00 起 C2 越温（C1 已闭合）——两票从不同时越温
    m(client, "m1", "2026-01-01T11:00:00+00:00", 9, "C1")
    m(client, "m2", "2026-01-01T12:00:00+00:00", 3, "C1")
    m(client, "m3", "2026-01-01T13:00:00+00:00", 9, "C2")
    m(client, "m4", "2026-01-01T14:00:00+00:00", 3, "C2")
    body = co_query(client, "T1",
                    "2026-01-01T09:00:00+00:00",
                    "2026-01-01T21:00:00+00:00").json()
    assert body["segments"] == []


# ------------------------------------------------------------- 参数校验


def test_co_excursion_invalid_min_cargo_count(client):
    post_trip(client)
    for bad in (0, 1, -3):
        r = co_query(client, "T1",
                     "2026-01-01T09:00:00+00:00",
                     "2026-01-01T21:00:00+00:00", min_count=bad)
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "invalid_min_cargo_count"


def test_co_excursion_invalid_query_window(client):
    post_trip(client)
    # 终点 == 起点
    r = co_query(client, "T1",
                 "2026-01-01T10:00:00+00:00", "2026-01-01T10:00:00+00:00")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_query_window"
    # 终点 < 起点
    r2 = co_query(client, "T1",
                  "2026-01-01T12:00:00+00:00", "2026-01-01T10:00:00+00:00")
    assert r2.status_code == 422
    assert r2.json()["error"]["code"] == "invalid_query_window"


def test_co_excursion_trip_not_found(client):
    r = co_query(client, "NOPE",
                 "2026-01-01T09:00:00+00:00", "2026-01-01T21:00:00+00:00")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "trip_not_found"


def test_co_excursion_timezone_offset_query_window(client):
    # 带时区偏移的查询起止：+08:00 的 20:00~22:00 即 UTC 12:00~14:00
    setup_two_cargo_staggered(client)
    body = co_query(client, "T1",
                    "2026-01-01T20:00:00+08:00",
                    "2026-01-01T22:00:00+08:00").json()
    assert body["query_start"] == "2026-01-01T12:00:00Z"
    assert body["query_end"] == "2026-01-01T14:00:00Z"
    assert body["segments"] == [
        {"start": "2026-01-01T12:00:00Z", "end": "2026-01-01T14:00:00Z",
         "cargo_ids": ["C1", "C2"], "cargo_count": 2}
    ]


# ------------------------------------------------- 裁剪：查询窗 / 装卸时段 / 未闭合


def test_co_excursion_clipped_by_query_window(client):
    setup_two_cargo_staggered(client)
    body = co_query(client, "T1",
                    "2026-01-01T13:00:00+00:00",
                    "2026-01-01T15:00:00+00:00").json()
    assert body["segments"] == [
        {"start": "2026-01-01T13:00:00Z", "end": "2026-01-01T14:00:00Z",
         "cargo_ids": ["C1", "C2"], "cargo_count": 2}
    ]


def test_open_excursion_clipped_to_query_end(client):
    make_cargo(client, "C1", "5")
    make_cargo(client, "C2", "6")
    post_trip(client)
    post_manifest(client, "T1", [load_10_20(client, "T1", "C1"),
                                 load_10_20(client, "T1", "C2")])
    # 11:00 车载 7℃：两票均越温，之后无闭合读数（区间未闭合）
    post_reading(client, "r1", "T1", "2026-01-01T11:00:00+00:00", 7)
    body = co_query(client, "T1",
                    "2026-01-01T10:00:00+00:00",
                    "2026-01-01T15:00:00+00:00").json()
    # 未闭合区间裁剪至查询终点 15:00
    assert body["segments"] == [
        {"start": "2026-01-01T11:00:00Z", "end": "2026-01-01T15:00:00Z",
         "cargo_ids": ["C1", "C2"], "cargo_count": 2}
    ]


def test_excursion_clipped_to_loading_window_half_open(client):
    make_cargo(client, "C1", "5")
    make_cargo(client, "C2", "5")
    post_trip(client)
    post_manifest(client, "T1", [
        load_10_20(client, "T1", "C1"),
        {"cargo_id": "C2",
         "loaded_at": "2026-01-01T10:00:00+00:00",
         "unloaded_at": "2026-01-01T12:00:00+00:00"},  # C2 中午即卸货
    ])
    post_reading(client, "r1", "T1", "2026-01-01T11:00:00+00:00", 7)  # 两票越温
    post_reading(client, "r2", "T1", "2026-01-01T16:00:00+00:00", 3)  # C1 闭合
    # C2 的越温区间未闭合，但其在本车次的装载时段止于 12:00（左闭右开）
    body = co_query(client, "T1",
                    "2026-01-01T09:00:00+00:00",
                    "2026-01-01T21:00:00+00:00").json()
    assert body["segments"] == [
        {"start": "2026-01-01T11:00:00Z", "end": "2026-01-01T12:00:00Z",
         "cargo_ids": ["C1", "C2"], "cargo_count": 2}
    ]


def test_adjacent_same_set_segments_merged_across_split_carriage(client):
    # C1 在同一车次上分段承运：[10,12) 与 [12,14)（首尾相接允许）；
    # 越温区间连续，合并结果应为一段而非两段
    make_cargo(client, "C1", "5")
    make_cargo(client, "C2", "5")
    post_trip(client)
    post_manifest(client, "T1", [
        {"cargo_id": "C1",
         "loaded_at": "2026-01-01T10:00:00+00:00",
         "unloaded_at": "2026-01-01T12:00:00+00:00"},
        {"cargo_id": "C1",
         "loaded_at": "2026-01-01T12:00:00+00:00",
         "unloaded_at": "2026-01-01T14:00:00+00:00"},
        {"cargo_id": "C2",
         "loaded_at": "2026-01-01T10:00:00+00:00",
         "unloaded_at": "2026-01-01T14:00:00+00:00"},
    ])
    post_reading(client, "r1", "T1", "2026-01-01T10:30:00+00:00", 7)  # 两票越温
    body = co_query(client, "T1",
                    "2026-01-01T10:00:00+00:00",
                    "2026-01-01T14:00:00+00:00").json()
    assert body["segments"] == [
        {"start": "2026-01-01T10:30:00Z", "end": "2026-01-01T14:00:00Z",
         "cargo_ids": ["C1", "C2"], "cargo_count": 2}
    ]


# ------------------------------------------------- 自有优先 / 晚到 / 跨车次


def test_own_measurement_precedence_changes_co_excursion(client):
    make_cargo(client, "C1", "5")
    make_cargo(client, "C2", "8")
    post_trip(client)
    post_manifest(client, "T1", [load_10_20(client, "T1", "C1"),
                                 load_10_20(client, "T1", "C2")])
    # 12:00 车载 9℃：C2 越温；C1 同刻自有测温 3℃ 优先 -> C1 不越温
    m(client, "m-own", "2026-01-01T12:00:00+00:00", 3, "C1")
    post_reading(client, "r1", "T1", "2026-01-01T12:00:00+00:00", 9)
    # 13:00 车载 9℃：C1 也越温（无自有测温）；14:00 车载 3℃ 双双闭合
    post_reading(client, "r2", "T1", "2026-01-01T13:00:00+00:00", 9)
    post_reading(client, "r3", "T1", "2026-01-01T14:00:00+00:00", 3)
    body = co_query(client, "T1",
                    "2026-01-01T09:00:00+00:00",
                    "2026-01-01T21:00:00+00:00").json()
    # 只有 [13,14) 两票同时越温（12:00 时 C1 采用自有 3℃ 未越温）
    assert body["segments"] == [
        {"start": "2026-01-01T13:00:00Z", "end": "2026-01-01T14:00:00Z",
         "cargo_ids": ["C1", "C2"], "cargo_count": 2}
    ]


def test_late_reading_reflected_in_subsequent_query(client):
    make_cargo(client, "C1", "5")
    make_cargo(client, "C2", "6")
    post_trip(client)
    post_manifest(client, "T1", [load_10_20(client, "T1", "C1"),
                                 load_10_20(client, "T1", "C2")])
    q = ("2026-01-01T10:00:00+00:00", "2026-01-01T20:00:00+00:00")
    assert co_query(client, "T1", *q).json()["segments"] == []
    # 晚到补录 11:00 的车载 7℃ 读数：两票越温区间重建为自 11:00 起未闭合
    post_reading(client, "vr-late", "T1", "2026-01-01T11:00:00+00:00", 7)
    body = co_query(client, "T1", *q).json()
    assert body["segments"] == [
        {"start": "2026-01-01T11:00:00Z", "end": "2026-01-01T20:00:00Z",
         "cargo_ids": ["C1", "C2"], "cargo_count": 2}
    ]


def test_cargo_not_on_this_trip_not_counted(client):
    make_cargo(client, "C1", "5")
    make_cargo(client, "C2", "5")
    make_cargo(client, "C3", "5")
    post_trip(client, "T1")
    post_trip(client, "T2")
    post_manifest(client, "T1", [load_10_20(client, "T1", "C1"),
                                 load_10_20(client, "T1", "C2")])
    post_manifest(client, "T2", [load_10_20(client, "T2", "C3")])
    # 三票同刻越温，但 C3 在另一车次上
    m(client, "m1", "2026-01-01T11:00:00+00:00", 9, "C1")
    m(client, "m2", "2026-01-01T11:00:00+00:00", 9, "C2")
    m(client, "m3", "2026-01-01T11:00:00+00:00", 9, "C3")
    body = co_query(client, "T1",
                    "2026-01-01T10:00:00+00:00",
                    "2026-01-01T20:00:00+00:00").json()
    assert [tuple(s["cargo_ids"]) for s in body["segments"]] == [("C1", "C2")]
    # T2 上只有 C3 一票，达不到门槛 2
    assert co_query(client, "T2",
                    "2026-01-01T10:00:00+00:00",
                    "2026-01-01T20:00:00+00:00").json()["segments"] == []


def test_trip_without_loadings_returns_empty(client):
    post_trip(client)
    body = co_query(client, "T1",
                    "2026-01-01T09:00:00+00:00",
                    "2026-01-01T21:00:00+00:00").json()
    assert body["segments"] == []


# ------------------------------------------------------------- 纯函数单测


def _dt(h, mi=0):
    return datetime(2026, 1, 1, h, mi, tzinfo=timezone.utc)


def test_simultaneous_segments_unit_merge_and_threshold():
    per_cargo = {
        "A": [(_dt(10), _dt(12)), (_dt(12), _dt(14))],  # 相邻同货物片段
        "B": [(_dt(11), _dt(13))],
        "C": [(_dt(11, 30), _dt(12, 30))],
    }
    segs = coexcursion.simultaneous_segments(per_cargo, 2)
    assert [(s.start, s.end, s.cargo_ids) for s in segs] == [
        (_dt(11), _dt(11, 30), ("A", "B")),
        (_dt(11, 30), _dt(12, 30), ("A", "B", "C")),
        (_dt(12, 30), _dt(13), ("A", "B")),
    ]
    # 门槛 3：只剩中段
    segs3 = coexcursion.simultaneous_segments(per_cargo, 3)
    assert [(s.start, s.end) for s in segs3] == [(_dt(11, 30), _dt(12, 30))]


def test_simultaneous_segments_unit_empty_and_single():
    assert coexcursion.simultaneous_segments({}, 2) == []
    assert coexcursion.simultaneous_segments({"A": [(_dt(1), _dt(2))]}, 2) == []
    # 集合在相邻基本段相同（A 的两段相邻）时合并为一段
    segs = coexcursion.simultaneous_segments(
        {"A": [(_dt(1), _dt(2)), (_dt(2), _dt(3))],
         "B": [(_dt(1), _dt(3))]}, 2)
    assert [(s.start, s.end, s.cargo_ids) for s in segs] == [
        (_dt(1), _dt(3), ("A", "B"))
    ]


def test_clip_cargo_excursions_unit_open_and_windows():
    class Ex:
        def __init__(self, start, end):
            self.start, self.end = start, end

    class Ld:
        def __init__(self, lo, un):
            self.loaded_at, self.unloaded_at = lo, un

    loadings = [Ld(_dt(10), _dt(12)), Ld(_dt(13), _dt(15))]
    excursions = [Ex(_dt(9), _dt(11)), Ex(_dt(14), None)]  # 第二个未闭合
    pieces = coexcursion.clip_cargo_excursions(
        excursions, loadings, _dt(0), _dt(16)
    )
    assert pieces == [(_dt(10), _dt(11)), (_dt(14), _dt(15))]
    # 查询窗进一步裁剪：未闭合区间裁到查询终点
    pieces2 = coexcursion.clip_cargo_excursions(
        excursions, loadings, _dt(10, 30), _dt(14, 30)
    )
    assert pieces2 == [(_dt(10, 30), _dt(11)), (_dt(14), _dt(14, 30))]
