"""同站分页 + 详情抓取。

约束：
* 只沿同站链接爬行（协议/主机/端口一致），跨站分页或详情链接跳过并记录；
* 分页跟随 ``rel=next``，带最大页数与死循环保护；
* 详情 URL 去重，抓取失败不中断整体查询；
* 每个失败都带上具体地址（:class:`CrawlIssue`），调用方不得据此宣称结果完整。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from .models import Announcement
from .parser import is_same_site, parse_detail, parse_detail_links, parse_next_page


@dataclass
class CrawlIssue:
    """单个抓取/解析失败。``url`` 始终是出错页面的绝对地址。"""

    url: str
    stage: str  # "list" | "detail" | "truncated"
    message: str


@dataclass
class CrawlResult:
    announcements: list[Announcement] = field(default_factory=list)
    list_pages: list[str] = field(default_factory=list)
    detail_pages: int = 0
    skipped_offsite: list[str] = field(default_factory=list)
    issues: list[CrawlIssue] = field(default_factory=list)
    truncated: bool = False

    @property
    def complete(self) -> bool:
        """所有页面都成功抓取并解析，且未因上限截断。"""
        return not self.issues and not self.truncated

    @property
    def errors(self) -> list[str]:
        """人类可读的错误列表，每条都带出错页面地址。"""
        labels = {"list": "列表页", "detail": "详情页", "truncated": "未抓取页面"}
        return [
            f"{labels.get(i.stage, i.stage)} {i.url}: {i.message}"
            for i in self.issues
        ]


class Crawler:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        max_list_pages: int = 50,
        max_detail_pages: int = 500,
    ) -> None:
        self._client = client
        self._max_list_pages = max_list_pages
        self._max_detail_pages = max_detail_pages

    async def _get(self, url: str) -> tuple[str | None, str | None]:
        """返回 (HTML, 错误信息)；成功时错误信息为 None。"""
        try:
            resp = await self._client.get(url, follow_redirects=True)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            return None, f"{type(exc).__name__}: {exc}"
        return resp.text, None

    def _issue(self, url: str, stage: str, message: str) -> None:
        self._issues.append(CrawlIssue(url=url, stage=stage, message=message))

    async def crawl(self, index_url: str) -> CrawlResult:
        result = CrawlResult()
        self._issues = result.issues
        seen_pages: set[str] = set()
        detail_urls: list[str] = []
        seen_details: set[str] = set()

        # 1) 抓列表分页（只走同站 rel=next）
        page_url: str | None = index_url
        while page_url and page_url not in seen_pages:
            if len(seen_pages) >= self._max_list_pages:
                # 仍有下一页未抓：结果因上限截断，不完整
                result.truncated = True
                self._issue(page_url, "truncated", f"超过列表页上限 {self._max_list_pages}，未抓取")
                break
            seen_pages.add(page_url)
            html, error = await self._get(page_url)
            if error is not None or html is None:
                self._issue(page_url, "list", error or "空响应")
                break
            result.list_pages.append(page_url)

            for detail_url, _id, _kind in parse_detail_links(html, page_url):
                if not is_same_site(detail_url, index_url):
                    result.skipped_offsite.append(detail_url)
                    continue
                if detail_url not in seen_details:
                    seen_details.add(detail_url)
                    detail_urls.append(detail_url)

            nxt = parse_next_page(html, page_url)
            if nxt and not is_same_site(nxt, index_url):
                result.skipped_offsite.append(nxt)
                nxt = None
            page_url = nxt

        if len(detail_urls) > self._max_detail_pages:
            result.truncated = True
            self._issue(
                index_url, "truncated",
                f"详情页数量 {len(detail_urls)} 超过上限 {self._max_detail_pages}，"
                f"只抓取前 {self._max_detail_pages} 个",
            )

        # 2) 抓详情（同站已保证），限量保护
        for detail_url in detail_urls[: self._max_detail_pages]:
            html, error = await self._get(detail_url)
            if error is not None or html is None:
                self._issue(detail_url, "detail", error or "空响应")
                continue
            result.detail_pages += 1
            try:
                result.announcements.append(parse_detail(html, detail_url))
            except ValueError as exc:
                self._issue(detail_url, "detail", f"解析失败: {exc}")

        return result
