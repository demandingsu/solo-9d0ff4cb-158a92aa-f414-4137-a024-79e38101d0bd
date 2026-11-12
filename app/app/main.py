"""FastAPI 入口：POST /query 采集并判定临时停运；POST /normalize 按线路编号归一。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException

from .crawler import CrawlResult, Crawler
from .engine import reconstruct
from .models import (
    NormalizeRequest,
    NormalizeResponse,
    QueryRequest,
    QueryResponse,
)
from .normalize import normalize_suspensions

logger = logging.getLogger("bus_notice_api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(10.0, connect=5.0),
        headers={"User-Agent": "bus-suspension-api/1.0"},
    ) as client:
        app.state.http_client = client
        yield


app = FastAPI(
    title="公交临时停运公告采集 API",
    version="1.1.0",
    description=(
        "从运营方分页发布的线路公告中，还原更正/撤销公告链，"
        "返回查询时刻仍有效的临时停运区间。\n\n"
        "``/normalize`` 额外按别名对照表把线路名称换算成线路编号，"
        "缺口或歧义区间单列待核实。"
    ),
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def _check_crawl(crawl: CrawlResult) -> None:
    if crawl.errors:
        logger.warning("抓取过程出现 %d 个错误: %s", len(crawl.errors), crawl.errors[:3])
    if not crawl.list_pages:
        raise HTTPException(
            status_code=502,
            detail={
                "message": "公告首页无法抓取或无任何分页内容",
                "errors": crawl.errors,
            },
        )


@app.post("/query", response_model=QueryResponse)
async def query(req: QueryRequest) -> QueryResponse:
    crawler = Crawler(app.state.http_client)
    crawl = await crawler.crawl(req.index_url)
    _check_crawl(crawl)

    result = reconstruct(crawl.announcements, req.query_time)

    return QueryResponse(
        index_url=req.index_url,
        query_time=req.query_time,
        list_pages_crawled=crawl.list_pages,
        detail_pages_crawled=crawl.detail_pages,
        announcements_parsed=len(crawl.announcements),
        active_suspensions=result.active,
        inactive_chains=result.inactive,
        unmatched_references=result.unmatched,
        skipped_offsite_links=crawl.skipped_offsite,
        crawl_errors=crawl.errors,
        result_complete=crawl.complete,
    )


@app.post("/normalize", response_model=NormalizeResponse)
async def normalize(req: NormalizeRequest) -> NormalizeResponse:
    crawler = Crawler(app.state.http_client)
    crawl = await crawler.crawl(req.index_url)
    _check_crawl(crawl)

    result = reconstruct(crawl.announcements, req.query_time)
    normalized = normalize_suspensions(result, req.query_time, req.aliases)

    return NormalizeResponse(
        index_url=req.index_url,
        query_time=req.query_time,
        list_pages_crawled=crawl.list_pages,
        detail_pages_crawled=crawl.detail_pages,
        announcements_parsed=len(crawl.announcements),
        normalized_suspensions=normalized.normalized,
        unresolved_segments=normalized.unresolved,
        inactive_chains=result.inactive,
        unmatched_references=result.unmatched,
        skipped_offsite_links=crawl.skipped_offsite,
        crawl_errors=crawl.errors,
        result_complete=crawl.complete,
    )
