"""领域数据模型。

所有时间在系统内部统一为时区感知的 ``datetime``（UTC 或带原始偏移量均可，
比较时 Python 会自动换算）。
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, field_validator, model_validator


def _require_aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} 必须带时区信息")
    return value


class NoticeKind(str, Enum):
    """公告类型。"""

    SUSPENSION = "suspension"          # 原始停运公告
    CORRECTION = "correction"          # 更正公告
    CANCELLATION = "cancellation"      # 撤销公告


class Announcement(BaseModel):
    """从详情页解析出的单条公告。"""

    notice_id: str = Field(..., description="公告编号，如 2026-001")
    route: str | None = Field(None, description="线路，如 12 路；更正/撤销页可能缺失")
    kind: NoticeKind
    suspend_start: datetime | None = None
    suspend_end: datetime | None = None
    published_at: datetime
    references: list[str] = Field(default_factory=list, description="更正/撤销引用的公告编号")
    url: str
    title: str | None = None

    @field_validator("published_at")
    @classmethod
    def _published_aware(cls, v: datetime) -> datetime:
        return _require_aware(v, "published_at")


class QueryRequest(BaseModel):
    """查询入参。"""

    index_url: str = Field(..., description="公告列表首页 URL（同站分页的起点）")
    query_time: datetime = Field(..., description="带时区的查询时刻，ISO 8601")

    @field_validator("index_url")
    @classmethod
    def _url_scheme(cls, v: str) -> str:
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("index_url 必须是 http/https URL")
        return v

    @field_validator("query_time")
    @classmethod
    def _query_aware(cls, v: datetime) -> datetime:
        return _require_aware(v, "query_time")


class ActiveSuspension(BaseModel):
    """查询时刻仍然有效的停运区间。"""

    route: str
    suspend_start: datetime
    suspend_end: datetime
    source_chain: list[str] = Field(..., description="形成该结论的公告链编号，根公告在前")
    sources: list[str] = Field(..., description="链上各公告详情页链接")
    published_at: datetime = Field(..., description="链上最新生效公告的发布时间")


class InactiveChain(BaseModel):
    """未产生有效停运的公告链，用于解释判定过程。"""

    root_id: str
    route: str | None
    reason: str = Field(..., description="cancelled / not_yet_effective / expired / no_interval")
    chain: list[str]
    sources: list[str]
    latest_published_at: datetime | None = None


class UnmatchedReference(BaseModel):
    """指向不存在公告的引用。"""

    notice_id: str = Field(..., description="发出引用的公告编号")
    missing_reference: str
    url: str
    kind: NoticeKind


class QueryResponse(BaseModel):
    """查询结果。"""

    index_url: str
    query_time: datetime
    list_pages_crawled: list[str]
    detail_pages_crawled: int
    announcements_parsed: int
    active_suspensions: list[ActiveSuspension]
    inactive_chains: list[InactiveChain]
    unmatched_references: list[UnmatchedReference]
    skipped_offsite_links: list[str] = Field(default_factory=list)
    crawl_errors: list[str] = Field(
        default_factory=list,
        description="抓取/解析失败的页面地址与原因；非空时结果不完整",
    )
    result_complete: bool = Field(
        True, description="所有公告页面均成功抓取解析才为 true"
    )


class RouteAlias(BaseModel):
    """运营方旧名/简称与线路编号的对照记录，只在 [effective_from, effective_to) 内有效。"""

    name: str = Field(..., description="公告中的线路名称，如 12路 / 十二路 / 老12路")
    route_id: str = Field(..., description="线路编号，如 12、B12")
    effective_from: datetime = Field(..., description="别名生效起始时刻（含）")
    effective_to: datetime | None = Field(
        None, description="别名生效截止时刻（不含）；null 表示长期有效"
    )

    @field_validator("name", "route_id")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("别名对照表的 name/route_id 不能为空")
        return v

    @field_validator("effective_from", "effective_to")
    @classmethod
    def _aware(cls, v: datetime | None) -> datetime | None:
        return v if v is None else _require_aware(v, "别名有效期")

    @model_validator(mode="after")
    def _ordered(self) -> RouteAlias:
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError("别名有效期为空或倒置：effective_to 必须晚于 effective_from")
        return self


class NormalizeRequest(BaseModel):
    """线路归一查询入参。"""

    index_url: str = Field(..., description="公告列表首页 URL（同站分页的起点）")
    query_time: datetime = Field(..., description="带时区的查询时刻，ISO 8601")
    aliases: list[RouteAlias] = Field(
        ..., description="别名对照表；同一名称重叠生效且指向不同编号时，对应区间单列待核实"
    )

    @field_validator("index_url")
    @classmethod
    def _url_scheme(cls, v: str) -> str:
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("index_url 必须是 http/https URL")
        return v

    @field_validator("query_time")
    @classmethod
    def _query_aware(cls, v: datetime) -> datetime:
        return _require_aware(v, "query_time")


class NormalizedSuspension(BaseModel):
    """切口后的一段停运，恰好对应一个线路编号。"""

    route_id: str = Field(..., description="归一后的线路编号")
    route_name: str = Field(..., description="公告链上采用的线路名称（别名原样）")
    suspend_start: datetime
    suspend_end: datetime
    source_chain: list[str] = Field(..., description="形成该结论的公告链编号，根公告在前")
    sources: list[str] = Field(..., description="链上各公告详情页链接")
    published_at: datetime = Field(..., description="链上最新生效公告的发布时间")


class UnresolvedSegment(BaseModel):
    """无法归一的切口段：名称缺口或同一段对应多个线路编号，需人工核实。"""

    reason: str = Field(
        ...,
        description="missing_alias（段内无有效对照）/ ambiguous_alias（段内对应多个编号）",
    )
    route_name: str | None = Field(None, description="公告链上采用的线路名称")
    candidate_route_ids: list[str] = Field(
        default_factory=list, description="ambiguous_alias 时为对照表给出的多个编号"
    )
    suspend_start: datetime
    suspend_end: datetime
    source_chain: list[str]
    sources: list[str]
    published_at: datetime


class NormalizeResponse(BaseModel):
    """线路归一查询结果。"""

    index_url: str
    query_time: datetime
    list_pages_crawled: list[str]
    detail_pages_crawled: int
    announcements_parsed: int
    normalized_suspensions: list[NormalizedSuspension]
    unresolved_segments: list[UnresolvedSegment]
    inactive_chains: list[InactiveChain]
    unmatched_references: list[UnmatchedReference]
    skipped_offsite_links: list[str] = Field(default_factory=list)
    crawl_errors: list[str] = Field(
        default_factory=list,
        description="抓取/解析失败的页面地址与原因；非空时归一结果不完整",
    )
    result_complete: bool = Field(
        True, description="所有公告页面均成功抓取解析才为 true；false 时不得视为完整归一"
    )

