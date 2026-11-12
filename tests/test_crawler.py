"""爬虫：失败页面必须带地址上报，且不得标记为完整。"""
from __future__ import annotations

import httpx
import pytest

from app.crawler import Crawler

pytestmark = pytest.mark.asyncio

INDEX = "http://site:8080/index.html"
PAGE2 = "http://site:8080/page2.html"
DETAIL_OK = "http://site:8080/notices/A-1.html"
DETAIL_FAIL = "http://site:8080/notices/A-2.html"

DETAIL_HTML = """<html><body>
<table class="notice-fields">
  <tr data-field="notice_id"><td>A-1</td></tr>
  <tr data-field="kind"><td>停运</td></tr>
  <tr data-field="route"><td>12 路</td></tr>
  <tr data-field="suspend_start"><td><time datetime="2026-11-10T06:00:00+08:00">x</time></td></tr>
  <tr data-field="suspend_end"><td><time datetime="2026-11-11T22:00:00+08:00">x</time></td></tr>
  <tr data-field="published_at"><td><time datetime="2026-11-08T09:00:00+08:00">x</time></td></tr>
</table></body></html>"""

INDEX_HTML = """<html><body>
<article class="notice" data-id="A-1">
  <a class="detail-link" href="notices/A-1.html">A-1</a></article>
<article class="notice" data-id="A-2">
  <a class="detail-link" href="notices/A-2.html">A-2</a></article>
<article class="notice" data-id="EXT">
  <a class="detail-link" href="http://external.example.org/x.html">EXT</a></article>
<nav class="pager"><a rel="next" href="page2.html">下一页</a></nav>
</body></html>"""


def handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if url == INDEX:
        return httpx.Response(200, text=INDEX_HTML)
    if url == PAGE2:
        return httpx.Response(500, text="boom")
    if url == DETAIL_OK:
        return httpx.Response(200, text=DETAIL_HTML)
    return httpx.Response(404, text="missing")


async def crawl() -> tuple[object, Crawler]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        crawler = Crawler(client)
        return await crawler.crawl(INDEX), crawler


async def test_partial_failure_reports_addresses_and_marks_incomplete():
    result, _ = await crawl()
    assert result.list_pages == [INDEX]
    assert [a.notice_id for a in result.announcements] == ["A-1"]
    assert result.skipped_offsite == ["http://external.example.org/x.html"]

    assert result.complete is False
    issue_urls = {i.url for i in result.issues}
    assert PAGE2 in issue_urls
    assert DETAIL_FAIL in issue_urls
    assert all(i.stage == "list" for i in result.issues if i.url == PAGE2)
    assert all(i.stage == "detail" for i in result.issues if i.url == DETAIL_FAIL)
    # 人类可读错误里必须出现地址
    assert any(PAGE2 in e for e in result.errors)
    assert any(DETAIL_FAIL in e for e in result.errors)


async def test_all_pages_ok_marks_complete():
    def ok_handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == INDEX:
            return httpx.Response(200, text=INDEX_HTML.replace(
                '<a rel="next" href="page2.html">下一页</a>', ""
            ))
        if str(request.url) == DETAIL_OK:
            return httpx.Response(200, text=DETAIL_HTML)
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(ok_handler)) as client:
        result = await Crawler(client).crawl(INDEX)
    # A-2 的 404 仍算失败（列表页确实声明了它），因此不完整
    assert result.complete is False
    assert any(DETAIL_FAIL in e for e in result.errors)


async def test_index_failure_has_no_pages_and_reports_url():
    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    async with httpx.AsyncClient(transport=httpx.MockTransport(failing)) as client:
        result = await Crawler(client).crawl(INDEX)
    assert result.list_pages == []
    assert result.complete is False
    assert result.issues[0].url == INDEX
    assert INDEX in result.errors[0]
