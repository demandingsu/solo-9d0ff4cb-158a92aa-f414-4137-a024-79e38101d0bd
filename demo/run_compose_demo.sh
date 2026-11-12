#!/usr/bin/env bash
# Docker Compose 离线演示：构建并启动 API + 本地示例公告站。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
QUERY_TIME="${1:-2026-11-11T10:00:00+08:00}"
ALIASES_FILE="$ROOT/site/html/aliases.json"

# 示例站镜像直接打包静态 HTML，先确保已生成（含 aliases.json 对照表）
python3 "$ROOT/site/build_site.py"

cd "$ROOT"
docker compose up -d --build

for _ in $(seq 1 40); do
  curl -sf http://127.0.0.1:8000/health >/dev/null && break
  sleep 1
done

# 用 Python 拼装 JSON，避免手工转义别名对照表
REQUEST="$(python3 - "$ALIASES_FILE" "$QUERY_TIME" <<'PY'
import json, sys
aliases = json.load(open(sys.argv[1], encoding="utf-8"))["aliases"]
print(json.dumps({
    "index_url": "http://notice-site:8080/index.html",
    "query_time": sys.argv[2],
    "aliases": aliases,
}, ensure_ascii=False))
PY
)"

echo "== POST /query（原始终端输出）=="
curl -s -X POST http://127.0.0.1:8000/query \
  -H 'Content-Type: application/json' \
  -d '{"index_url":"http://notice-site:8080/index.html","query_time":"'"$QUERY_TIME"'"}'
echo
echo "== POST /normalize（按线路编号归一）=="
curl -s -X POST http://127.0.0.1:8000/normalize \
  -H 'Content-Type: application/json' \
  -d "$REQUEST" | python3 -m json.tool

echo
echo "演示完成，停止服务: docker compose down"
