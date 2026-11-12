# 公交临时停运公告采集 API

调度员出车前，需要从运营方**分页发布**的线路公告中，查出“查询时刻仍有效”的临时停运。
本服务用 FastAPI + BeautifulSoup 抓取同站分页与详情页，按公告编号引用关系与发布时间
还原**更正/撤销公告链**，返回有效停运区间、来源链接及无法匹配的引用。

线路资料员还需要把运营方用**旧名/简称**发布的停运接入按**线路编号**汇总的数据表。
`POST /normalize` 在公告链判定之上叠加别名对照表，把有效停运区间按别名有效期切开，
逐段换算成线路编号；无法唯一确定的区间单列待核实，避免改名期间错配。

## 业务规则

| 规则 | 实现 |
| --- | --- |
| 公告按编号唯一标识 | `Announcement.notice_id` |
| 更正可引用另一条更正 | 引用图上从根（无有效出边引用的公告）沿后继遍历，支持任意深度 |
| 链顺序 | 引用关系定链，同一节点多个后继按发布时间排序 |
| 更正替换原停运信息 | 按发布时间依次应用，链上最后一条可见公告的起止/线路为当前生效信息 |
| 撤销使整条链失效 | 链上出现查询时刻已发布的撤销公告 → `inactive_chains[].reason=cancelled` |
| 未来公告不可见 | 发布时间晚于查询时刻的公告不参与链还原与判定 |
| 同线路独立公告分别判断 | 每条根公告独立成链；示例中 12 路有两条同时有效的独立停运 |
| 无法匹配的引用 | 被引用编号在整站不存在时收集到 `unmatched_references`，不影响其余判定 |
| 同站限制 | 只沿协议+主机+端口一致的链接爬行，跨站链接跳过并在 `skipped_offsite_links` 返回 |
| 抓取失败要可追溯 | 每个失败页面以绝对地址写入 `crawl_errors`；同时置 `result_complete=false`，任何结果都不得当作完整 |

停运区间按半开 `[suspend_start, suspend_end)` 判定：查询时刻等于结束时刻即视为已失效。
所有时间均要求带时区（ISO 8601 偏移），比较基于同一时间线瞬间。

## 线路归一规则（`POST /normalize`）

入参为 `index_url`、带时区的 `query_time`，以及别名对照表 `aliases[]`：
每条形如 `{"name": "开发区专线", "route_id": "17",
"effective_from": "...+08:00", "effective_to": "...+08:00" | null}`。

| 规则 | 实现 |
| --- | --- |
| 仅归一有效停运 | 先走公告链还原，只有查询时刻有效的 `active_suspensions` 进入归一 |
| 对照有效期是半开区间 | 对照记录在 `[effective_from, effective_to)` 内有效；`effective_to=null` 表示长期有效 |
| 按有效期切开停运区间 | 该名称所有对照记录的边界落在停运区间内即成为切点 |
| 每段只认一个编号 | 段内中点（同一时间线上的瞬间）查到唯一 `route_id` → `normalized_suspensions`，附 `source_chain`/`sources` |
| 缺口单列 | 段内没有任何有效对照记录 → `unresolved_segments[].reason=missing_alias`，**不用相邻段编号填补** |
| 歧义单列 | 段内重叠的有效记录指向不同编号 → `reason=ambiguous_alias`，`candidate_route_ids` 列出全部候选 |
| 名称缺口 | 公告链上没有线路名称 → 整段 `missing_alias` |
| 时间同刻比较 | 切点与中点均按 `datetime` 瞬间运算，“+00:00”与“+08:00”写法不影响结果 |
| 归一失败不丢来源 | 待核实段同样带公告链编号与来源链接，便于人工回溯 |

## 目录结构

```
app/                 FastAPI 采集服务
  app/models.py      Pydantic 模型（入参/出参/公告实体/别名对照）
  app/parser.py      BeautifulSoup 解析：分页 rel=next、详情链接、详情字段
  app/crawler.py     同站分页 + 详情抓取（httpx 异步，限量/去重/错误带地址）
  app/engine.py      公告链还原与有效性判定（纯函数，无 IO）
  app/normalize.py   别名有效期切分与线路编号归一（纯函数，无 IO）
  app/main.py        POST /query、POST /normalize、GET /health
  Dockerfile
site/                本地示例公告站（数据驱动生成纯静态 HTML）
  build_site.py      生成 html/（3 个列表页 + 17 条详情 + aliases.json）
  Dockerfile         python -m http.server 托管静态站
tests/               43 个单元/集成测试（pytest：解析、引擎、归一、爬虫、端到端）
demo/                本地与 Compose 演示脚本（均调用 /query 与 /normalize）
docker-compose.yml   api(8000) + notice-site(8080)
```

## 运行方式

### 方式一：Docker Compose（推荐，完整离线演示）

```bash
python3 site/build_site.py          # 生成示例站静态文件（含 aliases.json）
docker compose up -d --build
./demo/run_compose_demo.sh          # 或直接跑脚本：/query + /normalize 两条演示
```

手工发起归一查询（对照表可直接取示例站的 `aliases.json`）：

```bash
curl -s -X POST http://127.0.0.1:8000/normalize \
  -H 'Content-Type: application/json' \
  -d '{"index_url":"http://notice-site:8080/index.html",
       "query_time":"2026-11-11T10:00:00+08:00",
       "aliases":[{"name":"环线","route_id":"906",
                   "effective_from":"2026-11-01T00:00:00+08:00",
                   "effective_to":"2026-11-12T00:00:00+08:00"},
                  {"name":"环线","route_id":"907",
                   "effective_from":"2026-11-12T00:00:00+08:00",
                   "effective_to":null}]}'
```

容器内 API 通过 Compose 网络访问 `notice-site`；从宿主机调用时 `index_url`
也可换成 `http://127.0.0.1:8080/index.html`（两个地址同构，同站判定均成立）。
交互式文档：http://127.0.0.1:8000/docs

### 方式二：本地 venv（无 Docker 环境）

```bash
python3 -m venv --without-pip .venv
.venv/bin/python get-pip.py        # 若 ensurepip 不可用，先 bootstrap pip
.venv/bin/pip install -r app/requirements.txt
./demo/run_local_demo.sh           # 自动建站、起服务，并演示 /query 与 /normalize
```

### 测试

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q      # 需先在 127.0.0.1:8080 启动示例站
```

## 示例站点场景（查询时刻 2026-11-11 10:00 +08:00）

| 公告 | 场景 | 判定结果 |
| --- | --- | --- |
| N-2026-001 | 12 路原始停运 | 有效 |
| N-2026-002 | 18 路停运已结束 | `expired` |
| 003 → 006 → 010 | 25 路停运被两次更正（更正引用更正） | 有效，区间为 010 替换后的 11-10 ~ 11-20 |
| 004 → 007 | 33 路停运被撤销 | `cancelled`，整条链失效 |
| N-2026-005 | 12 路另一条独立停运；012 更正次日才发布 | 有效，链上只有 005 |
| N-2026-008 | 5 路停运尚未开始 | `not_yet_effective` |
| 009 → 011 | 40 路更正，且 011 误引用不存在的 N-2026-999 | 有效，同时报告悬空引用 |
| N-2026-013 | 50 路整条公告查询时刻之后才发布 | `not_yet_effective` |
| N-EXT-001 | 列表中的跨站链接 | 跳过，出现在 `skipped_offsite_links` |

### 归一示例（同一查询时刻 + 示例站 `aliases.json`）

| 公告 | 别名情况 | 归一结果 |
| --- | --- | --- |
| 001 / 005 | `12 路 → 12` 长期有效 | `route_id=12` 的标准记录（两条独立链分别保留） |
| 003 → 006 → 010 / 009 → 011 | 同名编号长期有效 | `route_id=25` / `40`，保留完整公告链与来源 |
| N-2026-014 | `环线`：906 至 11-12 00:00，之后 907 | 切成 906（11-11 05:00 ~ 11-12 00:00）与 907（11-12 00:00 ~ 11-13 22:00）两段 |
| N-2026-015 | `开发区专线` 仅 11-12 00:00 前有效 | 前段 `route_id=17`；11-12 00:00 之后 `missing_alias` 待核实 |
| N-2026-016 | `机场线` 的有效期重叠指向 A1/A2 | 不输出标准记录，`ambiguous_alias` 待核实，候选 A1、A2 |
| N-2026-017 | 公告写 `+00:00`，对照表写 `+08:00` | 按同一瞬间在 11-12 00:00+08 切开，901 / 902 |

待核实段不会借用相邻段的编号填补：如 015 的尾段只报告缺口，不会沿用前段的 17。

## 接入真实站点

真实页面结构不同时，只需替换 `app/parser.py` 中的 CSS 选择器与字段键映射
（`FIELD_KEYS` / `KIND_MAP`），引擎、爬虫与 API 契约无需改动。解析器约定详情页
使用 `<time datetime>` 携带带时区的 ISO 8601 时间、引用使用
`<a class="reference" data-ref-id="编号">`，避免依赖文案措辞。
