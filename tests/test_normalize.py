"""线路归一：别名有效期切分、缺口与歧义识别。"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.engine import reconstruct
from app.models import Announcement, NoticeKind, RouteAlias
from app.normalize import normalize_suspensions

TZ = "+08:00"
Q = datetime.fromisoformat(f"2026-11-11T10:00:00{TZ}")


def dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def make(
    notice_id: str,
    published: str,
    route: str | None,
    start: str,
    end: str,
    kind: NoticeKind = NoticeKind.SUSPENSION,
    refs: list[str] | None = None,
) -> Announcement:
    return Announcement(
        notice_id=notice_id,
        route=route,
        kind=kind,
        suspend_start=dt(start),
        suspend_end=dt(end),
        published_at=dt(published),
        references=refs or [],
        url=f"http://x/notices/{notice_id}.html",
    )


def alias(name: str, route_id: str, start: str, end: str | None = None) -> RouteAlias:
    return RouteAlias(
        name=name, route_id=route_id, effective_from=dt(start),
        effective_to=dt(end) if end else None,
    )


def norm(anns: list[Announcement], aliases: list[RouteAlias], q: datetime = Q):
    return normalize_suspensions(reconstruct(anns, q), q, aliases)


def test_single_name_single_id_keeps_chain_and_sources():
    anns = [
        make("A1", f"2026-11-09T09:00:00{TZ}", "12 路",
             f"2026-11-10T06:00:00{TZ}", f"2026-11-12T22:00:00{TZ}"),
        make("A2", f"2026-11-10T09:00:00{TZ}", "12 路",
             f"2026-11-10T06:00:00{TZ}", f"2026-11-13T22:00:00{TZ}",
             kind=NoticeKind.CORRECTION, refs=["A1"]),
    ]
    res = norm(anns, [alias("12 路", "12", "2026-01-01T00:00:00+08:00")])
    assert len(res.normalized) == 1
    item = res.normalized[0]
    assert item.route_id == "12"
    assert item.source_chain == ["A1", "A2"]
    assert item.sources == ["http://x/notices/A1.html", "http://x/notices/A2.html"]
    assert item.suspend_end == dt(f"2026-11-13T22:00:00{TZ}")


def test_interval_split_at_alias_boundary():
    # 旧名有效至 11-11 18:00，新名从 11-11 18:00 起
    anns = [
        make("A1", f"2026-11-08T09:00:00{TZ}", "老名",
             f"2026-11-10T06:00:00{TZ}", f"2026-11-13T22:00:00{TZ}"),
    ]
    aliases = [
        alias("老名", "100", "2026-11-01T00:00:00+08:00", "2026-11-11T18:00:00+08:00"),
        alias("新名", "100", "2026-11-11T18:00:00+08:00"),
    ]
    res = norm(anns, aliases)
    # 公告只写了一个名称，按该名称的对照有效期切开；切点处旧名失效 → 后段缺口
    assert [s.route_id for s in res.normalized] == ["100"]
    assert res.normalized[0].suspend_start == dt(f"2026-11-10T06:00:00{TZ}")
    assert res.normalized[0].suspend_end == dt(f"2026-11-11T18:00:00{TZ}")
    assert len(res.unresolved) == 1
    gap = res.unresolved[0]
    assert gap.reason == "missing_alias"
    assert gap.suspend_start == dt(f"2026-11-11T18:00:00{TZ}")
    assert gap.suspend_end == dt(f"2026-11-13T22:00:00{TZ}")
    assert gap.source_chain == ["A1"]


def test_same_name_two_non_overlapping_ids_over_time():
    anns = [
        make("A1", f"2026-11-08T09:00:00{TZ}", "环线",
             f"2026-11-10T06:00:00{TZ}", f"2026-11-13T22:00:00{TZ}"),
    ]
    aliases = [
        alias("环线", "906", "2026-11-01T00:00:00+08:00", "2026-11-12T00:00:00+08:00"),
        alias("环线", "907", "2026-11-12T00:00:00+08:00"),
    ]
    res = norm(anns, aliases)
    assert [s.route_id for s in res.normalized] == ["906", "907"]
    assert res.normalized[0].suspend_end == dt(f"2026-11-12T00:00:00{TZ}")
    assert res.normalized[0].suspend_end == res.normalized[1].suspend_start
    assert res.unresolved == []


def test_overlapping_aliases_to_different_ids_are_unresolved():
    anns = [
        make("A1", f"2026-11-08T09:00:00{TZ}", "机场线",
             f"2026-11-10T06:00:00{TZ}", f"2026-11-13T22:00:00{TZ}"),
    ]
    aliases = [
        alias("机场线", "A1", "2026-11-10T00:00:00+08:00", "2026-11-15T00:00:00+08:00"),
        alias("机场线", "A2", "2026-11-10T00:00:00+08:00", "2026-11-15T00:00:00+08:00"),
    ]
    res = norm(anns, aliases)
    assert res.normalized == []
    assert len(res.unresolved) == 1
    item = res.unresolved[0]
    assert item.reason == "ambiguous_alias"
    assert item.candidate_route_ids == ["A1", "A2"]
    assert item.sources == ["http://x/notices/A1.html"]


def test_overlapping_aliases_to_same_id_are_not_ambiguous():
    anns = [
        make("A1", f"2026-11-08T09:00:00{TZ}", "老12路",
             f"2026-11-10T06:00:00{TZ}", f"2026-11-13T22:00:00{TZ}"),
    ]
    aliases = [
        alias("老12路", "12", "2026-11-01T00:00:00+08:00", "2026-11-20T00:00:00+08:00"),
        alias("老12路", "12", "2026-11-05T00:00:00+08:00"),
    ]
    res = norm(anns, aliases)
    assert [s.route_id for s in res.normalized] == ["12"]
    assert res.unresolved == []


def test_gap_is_not_filled_from_adjacent_segment():
    # 中间一段（11-11 12:00 ~ 11-11 20:00）没有任何有效对照记录
    anns = [
        make("A1", f"2026-11-08T09:00:00{TZ}", "测试线",
             f"2026-11-10T06:00:00{TZ}", f"2026-11-13T22:00:00{TZ}"),
    ]
    aliases = [
        alias("测试线", "1", "2026-11-01T00:00:00+08:00", "2026-11-11T12:00:00+08:00"),
        alias("测试线", "2", "2026-11-11T20:00:00+08:00"),
    ]
    res = norm(anns, aliases)
    assert [s.route_id for s in res.normalized] == ["1", "2"]
    gap = res.unresolved[0]
    assert gap.reason == "missing_alias"
    assert gap.suspend_start == dt(f"2026-11-11T12:00:00{TZ}")
    assert gap.suspend_end == dt(f"2026-11-11T20:00:00{TZ}")
    assert gap.candidate_route_ids == []


def test_missing_route_name_is_unresolved():
    anns = [
        make("A1", f"2026-11-08T09:00:00{TZ}", None,
             f"2026-11-10T06:00:00{TZ}", f"2026-11-13T22:00:00{TZ}"),
    ]
    res = norm(anns, [alias("12 路", "12", "2026-11-01T00:00:00+08:00")])
    assert res.normalized == []
    assert res.unresolved[0].reason == "missing_alias"
    assert res.unresolved[0].route_name is None


def test_only_active_suspensions_are_normalized():
    anns = [
        make("OLD", f"2026-11-01T09:00:00{TZ}", "12 路",
             f"2026-11-02T06:00:00{TZ}", f"2026-11-03T22:00:00{TZ}"),
        make("ACT", f"2026-11-08T09:00:00{TZ}", "12 路",
             f"2026-11-10T06:00:00{TZ}", f"2026-11-13T22:00:00{TZ}"),
    ]
    res = norm(anns, [alias("12 路", "12", "2026-11-01T00:00:00+08:00")])
    assert [s.source_chain for s in res.normalized] == [["ACT"]]


def test_alias_bounds_are_half_open():
    # 切点 11-11 10:00 恰为查询时刻；段内中点决定归属，边界本身按半开处理
    anns = [
        make("A1", f"2026-11-08T09:00:00{TZ}", "边界线",
             f"2026-11-10T06:00:00{TZ}", f"2026-11-13T22:00:00{TZ}"),
    ]
    aliases = [
        alias("边界线", "1", "2026-11-01T00:00:00+08:00", "2026-11-11T10:00:00+08:00"),
        alias("边界线", "2", "2026-11-11T10:00:00+08:00"),
    ]
    res = norm(anns, aliases)
    assert [s.route_id for s in res.normalized] == ["1", "2"]
    assert res.normalized[0].suspend_end == dt(f"2026-11-11T10:00:00{TZ}")
    assert res.normalized[1].suspend_start == dt(f"2026-11-11T10:00:00{TZ}")


def test_mixed_offsets_compare_as_same_instant():
    # 停运区间用 UTC 写，对照表边界用 +08:00 写；切点仍落在同一瞬间
    anns = [
        make("A1", "2026-11-08T09:00:00+08:00", "跨时区线",
             "2026-11-11T01:00:00+00:00", "2026-11-12T00:00:00+00:00"),
    ]
    aliases = [
        alias("跨时区线", "901", "2026-11-01T00:00:00+00:00",
              "2026-11-11T16:00:00+00:00"),
        alias("跨时区线", "902", "2026-11-11T16:00:00+00:00"),
    ]
    res = norm(anns, aliases)
    assert [s.route_id for s in res.normalized] == ["901", "902"]
    boundary = res.normalized[0].suspend_end
    assert boundary == res.normalized[1].suspend_start
    assert boundary == dt("2026-11-11T16:00:00+00:00")
    assert boundary == dt("2026-11-12T00:00:00+08:00")


def test_adjacent_segments_with_same_id_are_merged():
    anns = [
        make("A1", f"2026-11-08T09:00:00{TZ}", "同名线",
             f"2026-11-10T06:00:00{TZ}", f"2026-11-13T22:00:00{TZ}"),
    ]
    aliases = [
        alias("同名线", "7", "2026-11-01T00:00:00+08:00", "2026-11-11T12:00:00+08:00"),
        alias("同名线", "7", "2026-11-11T12:00:00+08:00", "2026-11-12T00:00:00+08:00"),
        alias("同名线", "7", "2026-11-12T00:00:00+08:00"),
    ]
    res = norm(anns, aliases)
    assert len(res.normalized) == 1
    assert res.normalized[0].suspend_start == dt(f"2026-11-10T06:00:00{TZ}")
    assert res.normalized[0].suspend_end == dt(f"2026-11-13T22:00:00{TZ}")


def test_adjacent_different_ids_are_not_merged():
    anns = [
        make("A1", f"2026-11-08T09:00:00{TZ}", "同名线",
             f"2026-11-10T06:00:00{TZ}", f"2026-11-13T22:00:00{TZ}"),
    ]
    aliases = [
        alias("同名线", "7", "2026-11-01T00:00:00+08:00", "2026-11-11T12:00:00+08:00"),
        alias("同名线", "8", "2026-11-11T12:00:00+08:00"),
    ]
    res = norm(anns, aliases)
    assert [s.route_id for s in res.normalized] == ["7", "8"]


def test_unresolved_route_alias_inverted_range_rejected():
    with pytest.raises(Exception):
        RouteAlias(
            name="X", route_id="1",
            effective_from=dt("2026-11-12T00:00:00+08:00"),
            effective_to=dt("2026-11-11T00:00:00+08:00"),
        )


def test_alias_name_whitespace_ignored():
    anns = [
        make("A1", f"2026-11-08T09:00:00{TZ}", "12  路",
             f"2026-11-10T06:00:00{TZ}", f"2026-11-13T22:00:00{TZ}"),
    ]
    res = norm(anns, [alias("12路", "12", "2026-11-01T00:00:00+08:00")])
    assert [s.route_id for s in res.normalized] == ["12"]
    assert res.normalized[0].route_name == "12  路"
