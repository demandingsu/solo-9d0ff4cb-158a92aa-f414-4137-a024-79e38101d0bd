"""线路归一：把公告链上的线路名称按别名对照表换算成线路编号。

规则：

* 只对查询时刻仍然有效的停运区间做归一（有效性已由 ``engine.reconstruct`` 判定）。
* 对照表记录只在 ``[effective_from, effective_to)`` 内有效；同一线路名称在
  某一瞬间的全部有效记录构成该瞬间的编号候选。
* 停运区间按对照表有效期切开；每段用段内中点（同一时间线上的真实瞬间）查
  对照表，缺口段绝不借用相邻段的编号。
* 一段只对应一个线路编号 → 标准记录（保留公告链与来源链接）；
  段内无有效对照记录（缺口）或重叠的有效记录指向不同编号 → 待核实记录。
* 对照表是参照数据：已经在查询时刻生效的停运，其未来区段按对照表的未来
  有效期归一，而不是把整段都按查询时刻的旧名处理。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from .engine import ReconstructionResult
from .models import (
    ActiveSuspension,
    NormalizedSuspension,
    RouteAlias,
    UnresolvedSegment,
)

UNKNOWN_ROUTE = "(未知线路)"


@dataclass
class NormalizeResult:
    normalized: list[NormalizedSuspension]
    unresolved: list[UnresolvedSegment]


def _name_key(name: str) -> str:
    """名称比较键：忽略空白差异（“12 路”与“12路”视为同一名称）。"""
    return re.sub(r"\s+", "", name)


def _candidate_ids(name: str, at: datetime, aliases: list[RouteAlias]) -> list[str]:
    """名称在某一瞬间对应的线路编号（去重排序）；返回多个即歧义。"""
    key = _name_key(name)
    return sorted(
        {
            a.route_id
            for a in aliases
            if _name_key(a.name) == key
            and a.effective_from <= at
            and (a.effective_to is None or at < a.effective_to)
        }
    )


def _cut_points(
    start: datetime, end: datetime, name: str, aliases: list[RouteAlias]
) -> list[datetime]:
    """该名称所有对照记录的有效期边界中，落在 (start, end) 内的切点。"""
    key = _name_key(name)
    points = {start, end}
    for a in aliases:
        if _name_key(a.name) != key:
            continue
        for boundary in (a.effective_from, a.effective_to):
            if boundary is not None and start < boundary < end:
                points.add(boundary)
    return sorted(points)


def normalize_suspensions(
    result: ReconstructionResult,
    query_time: datetime,
    aliases: list[RouteAlias],
) -> NormalizeResult:
    """对查询时刻有效的停运链做线路编号归一与区间切割。"""
    normalized: list[NormalizedSuspension] = []
    unresolved: list[UnresolvedSegment] = []

    for active in result.active:
        _normalize_one(active, aliases, normalized, unresolved)

    normalized.sort(key=lambda x: (x.route_id, x.suspend_start, tuple(x.source_chain)))
    unresolved.sort(
        key=lambda x: (x.suspend_start, x.route_name or "", tuple(x.source_chain))
    )
    return NormalizeResult(normalized=normalized, unresolved=unresolved)


def _base_fields(active: ActiveSuspension, start: datetime, end: datetime) -> dict:
    return dict(
        route_name=active.route,
        suspend_start=start,
        suspend_end=end,
        source_chain=active.source_chain,
        sources=active.sources,
        published_at=active.published_at,
    )


def _append_normalized(
    out: list[NormalizedSuspension], item: NormalizedSuspension
) -> None:
    """相邻且编号相同的段合并，避免对照表内部边界造成碎片记录。"""
    if (
        out
        and out[-1].route_id == item.route_id
        and out[-1].source_chain == item.source_chain
        and out[-1].suspend_end == item.suspend_start
    ):
        out[-1] = out[-1].model_copy(update={"suspend_end": item.suspend_end})
        return
    out.append(item)


def _normalize_one(
    active: ActiveSuspension,
    aliases: list[RouteAlias],
    normalized: list[NormalizedSuspension],
    unresolved: list[UnresolvedSegment],
) -> None:
    name = active.route
    if not name or name == UNKNOWN_ROUTE:
        # 公告链没有可用的线路名称：整段待核实
        unresolved.append(
            UnresolvedSegment(
                reason="missing_alias",
                route_name=None,
                suspend_start=active.suspend_start,
                suspend_end=active.suspend_end,
                source_chain=active.source_chain,
                sources=active.sources,
                published_at=active.published_at,
            )
        )
        return

    cuts = _cut_points(
        active.suspend_start, active.suspend_end, name, aliases
    )
    for seg_start, seg_end in zip(cuts, cuts[1:]):
        if seg_start >= seg_end:
            continue
        # 段内中点：比较基于同一时间线的瞬间，不受原始偏移写法影响
        midpoint = seg_start + (seg_end - seg_start) / 2
        candidates = _candidate_ids(name, midpoint, aliases)
        base = _base_fields(active, seg_start, seg_end)

        if len(candidates) == 1:
            _append_normalized(
                normalized, NormalizedSuspension(route_id=candidates[0], **base)
            )
        elif candidates:
            # 重叠有效期指向多个编号：不猜，单列待核实
            unresolved.append(
                UnresolvedSegment(
                    reason="ambiguous_alias",
                    candidate_route_ids=candidates,
                    **base,
                )
            )
        else:
            # 缺口：不借用相邻段的编号
            unresolved.append(UnresolvedSegment(reason="missing_alias", **base))
