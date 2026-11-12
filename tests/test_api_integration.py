"""端到端：FastAPI + 爬虫 + 引擎，对本地示例公告站做完整查询。

前置：示例站点已在 127.0.0.1:8080 提供服务（见 README / demo 脚本）。
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app

SITE = os.environ.get("TEST_SITE_URL", "http://127.0.0.1:8080/index.html")
ALIASES_FILE = Path(__file__).parent.parent / "site" / "html" / "aliases.json"
Q = "2026-11-11T10:00:00+08:00"

pytestmark = pytest.mark.asyncio


def load_aliases() -> list[dict]:
    return json.loads(ALIASES_FILE.read_text(encoding="utf-8"))["aliases"]


async def post(payload: dict, path: str = "/query") -> tuple[int, dict]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # lifespan 由 LifespanManager 之外的方式不保证执行，手动触发
        async with app.router.lifespan_context(app):
            r = await ac.post(path, json=payload)
    return r.status_code, r.json()


async def normalize(payload: dict) -> tuple[int, dict]:
    return await post(payload, path="/normalize")


async def test_full_query_workflow():
    status, data = await post({"index_url": SITE, "query_time": Q})
    assert status == 200, data

    # 三个列表页全部爬到，17 条同站详情（跨站 1 条被跳过）
    assert len(data["list_pages_crawled"]) == 3
    assert data["detail_pages_crawled"] == 17
    assert data["announcements_parsed"] == 17
    assert len(data["skipped_offsite_links"]) == 1
    assert data["crawl_errors"] == []
    assert data["result_complete"] is True

    active = data["active_suspensions"]
    chains = {tuple(a["source_chain"]) for a in active}
    # 有效链：001 原始；005 原始（012 次日更正不可见）；
    # 003→006→010 两次更正；009→011 更正（带悬空引用）
    assert ("N-2026-001",) in chains
    assert ("N-2026-005",) in chains
    assert ("N-2026-003", "N-2026-006", "N-2026-010") in chains
    assert ("N-2026-009", "N-2026-011") in chains

    by_route = {a["route"]: a for a in active}
    # 两次更正后的最终区间
    assert by_route["25 路"]["suspend_end"].startswith("2026-11-20T22:00:00")
    # 同线路两条独立公告分别判断：12 路有两条有效停运
    routes_12 = [a for a in active if a["route"] == "12 路"]
    assert len(routes_12) == 2

    reasons = {(c["root_id"], c["reason"]) for c in data["inactive_chains"]}
    assert ("N-2026-002", "expired") in reasons
    assert ("N-2026-004", "cancelled") in reasons
    assert ("N-2026-008", "not_yet_effective") in reasons
    assert ("N-2026-013", "not_yet_effective") in reasons

    unmatched = data["unmatched_references"]
    assert len(unmatched) == 1
    assert unmatched[0]["notice_id"] == "N-2026-011"
    assert unmatched[0]["missing_reference"] == "N-2026-999"

    # 来源链接完整
    chain25 = next(a for a in active if a["route"] == "25 路")
    assert all(u.startswith("http://127.0.0.1:8080/notices/") for u in chain25["sources"])


async def test_naive_query_time_rejected():
    status, _ = await post({"index_url": SITE, "query_time": "2026-11-11T10:00:00"})
    assert status == 422


async def test_bad_index_url_returns_502():
    status, data = await post(
        {"index_url": "http://127.0.0.1:9/never.html", "query_time": Q}
    )
    assert status == 502


async def test_normalize_splits_interval_across_rename_boundary():
    status, data = await normalize(
        {"index_url": SITE, "query_time": Q, "aliases": load_aliases()}
    )
    assert status == 200, data
    assert data["result_complete"] is True
    assert data["crawl_errors"] == []

    # 环线 014：11-11 05:00 ~ 11-13 22:00，在 11-12 00:00 改名处切成两段
    loop = [
        s for s in data["normalized_suspensions"]
        if s["source_chain"] == ["N-2026-014"]
    ]
    assert [s["route_id"] for s in loop] == ["906", "907"]
    assert loop[0]["suspend_end"] == loop[1]["suspend_start"]
    assert loop[0]["suspend_start"].startswith("2026-11-11T05:00:00")
    assert loop[1]["suspend_end"].startswith("2026-11-13T22:00:00")
    assert loop[0]["route_name"] == "环线"
    assert all(u.endswith("/notices/N-2026-014.html") for u in loop[0]["sources"])

    # 夜班专线 017 用 UTC 偏移书写：边界按同一瞬间切开
    night = [
        s for s in data["normalized_suspensions"]
        if s["source_chain"] == ["N-2026-017"]
    ]
    assert [s["route_id"] for s in night] == ["901", "902"]
    boundary = datetime.fromisoformat(night[0]["suspend_end"])
    # 11-11T16:00Z == 11-12T00:00+08:00，两种写法是同一瞬间
    assert boundary == datetime.fromisoformat("2026-11-12T00:00:00+08:00")
    assert datetime.fromisoformat(night[1]["suspend_start"]) == boundary

    # 缺口与歧义段不进入标准记录，单列待核实
    unresolved = data["unresolved_segments"]
    gap = [u for u in unresolved if u["source_chain"] == ["N-2026-015"]]
    assert len(gap) == 1
    assert gap[0]["reason"] == "missing_alias"
    # 11-11 05:00 至 11-12 00:00 有编号，之后才是缺口
    assert gap[0]["suspend_start"].startswith("2026-11-12T00:00:00")

    ambiguous = [u for u in unresolved if u["source_chain"] == ["N-2026-016"]]
    assert len(ambiguous) == 1
    assert ambiguous[0]["reason"] == "ambiguous_alias"
    assert ambiguous[0]["candidate_route_ids"] == ["A1", "A2"]

    # 每段只输出一个编号；标准化记录都带来源链接
    for s in data["normalized_suspensions"]:
        assert s["route_id"]
        assert s["sources"]
    by_ids = {
        s["source_chain"][0] for s in data["normalized_suspensions"]
    }
    assert {"N-2026-001", "N-2026-005", "N-2026-003", "N-2026-009"} <= by_ids


async def test_normalize_rejects_naive_alias_bounds():
    aliases = load_aliases()
    aliases[0]["effective_from"] = "2026-11-01T00:00:00"
    status, _ = await normalize(
        {"index_url": SITE, "query_time": Q, "aliases": aliases}
    )
    assert status == 422


async def test_normalize_rejects_inverted_alias_range():
    status, _ = await normalize(
        {
            "index_url": SITE,
            "query_time": Q,
            "aliases": [
                {
                    "name": "X",
                    "route_id": "1",
                    "effective_from": "2026-11-12T00:00:00+08:00",
                    "effective_to": "2026-11-11T00:00:00+08:00",
                }
            ],
        }
    )
    assert status == 422


async def test_normalize_reports_partial_crawl():
    # 指向示例站的非法端口：首页失败 → 502，错误里带地址
    status, data = await normalize(
        {
            "index_url": "http://127.0.0.1:9/index.html",
            "query_time": Q,
            "aliases": load_aliases(),
        }
    )
    assert status == 502
    assert "http://127.0.0.1:9/index.html" in json.dumps(data, ensure_ascii=False)
