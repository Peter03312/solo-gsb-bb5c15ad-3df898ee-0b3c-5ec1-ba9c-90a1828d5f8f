"""同一车次多票货物的**同时越温时段**计算（纯函数，便于单测）。

质控场景：一个冷链车次承载多票货物，质控人员给定查询窗与数量门槛，
查找“车上同时有 >= 门槛票数货物处于越温状态”的时段。

计算步骤
--------
1. 每票货物在**现有合并读数时间线**上重建越温区间（见 app/timeline.py /
   app/chain.py：车载读数仅作用于读数时刻仍在车上的货物，同刻自有测温优先）；
2. 每个越温区间与该货物**在本车次的装卸时段**（左闭右开）及**查询窗**相交，
   未闭合区间的终点裁剪至查询终点，只保留正长度片段；
3. 把所有货物的越温片段做扫描线合并：货物集合变化即分段，
   相邻集合相同的片段合并，最终只保留达到数量门槛的片段。

输出为按时间升序、互不重叠的半开区间 ``[start, end)`` 及当段越温货物编号。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CoSegment:
    """一段同时越温时段：半开区间 [start, end) 与当段越温货物编号（升序）。"""

    start: object
    end: object
    cargo_ids: tuple[str, ...]


def clip_cargo_excursions(
    excursions,
    loadings,
    query_start,
    query_end,
) -> list[tuple]:
    """一票货物在车次上的越温片段：越温区间 ∩ 本车次装卸时段 ∩ 查询窗。

    * ``excursions`` 为该货物在合并时间线上重建出的全部越温区间
      （``end=None`` 表示未闭合，裁剪至查询终点 ``query_end``）；
    * ``loadings`` 只含该货物**在本车次**的装卸时段（左闭右开）；
    * 返回正长度半开片段 ``(start, end)`` 列表，可能为空。
    """
    pieces: list[tuple] = []
    for ex in excursions:
        # 未闭合区间：裁剪至查询终点（查询窗之外的部分本就不关心）
        ex_end = ex.end if ex.end is not None else query_end
        for l in loadings:
            start = max(ex.start, l.loaded_at, query_start)
            end = min(ex_end, l.unloaded_at, query_end)
            if start < end:
                pieces.append((start, end))
    return pieces


def simultaneous_segments(per_cargo: dict, min_count: int) -> list[CoSegment]:
    """扫描线合并各票货物的越温片段，返回达到门槛的同时越温时段。

    :param per_cargo: ``货物编号 -> [(start, end), ...]``，片段为半开区间、
        正长度、且已裁剪到查询窗与本车次装卸时段内。
    :param min_count: 数量门槛（同时越温票数达到该值的片段才保留）。
    :return: 按时间升序、互不重叠的 :class:`CoSegment` 列表；
        货物集合变化即分段，相邻集合相同的片段已合并。
    """
    boundaries = sorted(
        {t for pieces in per_cargo.values() for seg in pieces for t in seg}
    )
    result: list[CoSegment] = []
    current_set: frozenset | None = None
    current_start = None

    def flush(upto) -> None:
        nonlocal current_set, current_start
        if (
            current_set is not None
            and len(current_set) >= min_count
            and current_start < upto
        ):
            result.append(
                CoSegment(current_start, upto, tuple(sorted(current_set)))
            )

    for i in range(len(boundaries) - 1):
        a, b = boundaries[i], boundaries[i + 1]
        # [a, b) 上各货物是否越温为常量：统计覆盖该基本段的货物集合
        active = frozenset(
            cargo_id
            for cargo_id, pieces in per_cargo.items()
            if any(s <= a and e >= b for s, e in pieces)
        )
        if active == current_set:
            continue  # 相邻集合相同：并入当前片段
        flush(a)  # 集合变化：结算上一段
        current_set = active
        current_start = a
    if boundaries:
        flush(boundaries[-1])
    return result
