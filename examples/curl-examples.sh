#!/usr/bin/env bash
# 博物馆展柜温度暴露审定服务 —— curl 端到端调用示例
# 使用前：docker compose up --build -d
set -euo pipefail

BASE=${BASE:-http://localhost:8000}
j() { python3 -m json.tool; }

echo "== 健康检查 =="
curl -s "$BASE/health"; echo

echo "== 1. 创建审定窗口 =="
WID=$(curl -s -X POST "$BASE/windows" -H 'Content-Type: application/json' -d '{
  "start": "2026-09-01T08:00:00Z",
  "end":   "2026-09-01T12:00:00Z",
  "temp_min": 18.0,
  "temp_max": 22.0,
  "max_continuous_exceed_seconds": 900,
  "max_sampling_interval_seconds": 1800
}' | tee /dev/stderr | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
echo "window_id=$WID"

echo "== 2. 提交读数 =="
post_reading() {
  curl -s -X POST "$BASE/windows/$WID/readings" -H 'Content-Type: application/json' \
    -d "{\"reading_id\":\"$1\",\"revision\":$2,\"timestamp\":\"$3\",\"temperature\":$4}" | j
}
post_reading r1 1 2026-09-01T08:00:00Z 20.1
post_reading r2 1 2026-09-01T08:30:00Z 20.4
post_reading r3 1 2026-09-01T09:00:00Z 22.8
post_reading r4 1 2026-09-01T09:30:00Z 23.1
post_reading r5 1 2026-09-01T10:00:00Z 20.0
post_reading r6 1 2026-09-01T11:00:00Z 20.0
post_reading r7 1 2026-09-01T12:00:00Z 20.0

echo "== 3. 幂等重提（200 idempotent）与内容冲突（409）=="
curl -s -o /dev/null -w "idempotent -> %{http_code}\n" -X POST "$BASE/windows/$WID/readings" \
  -H 'Content-Type: application/json' \
  -d '{"reading_id":"r1","revision":1,"timestamp":"2026-09-01T08:00:00Z","temperature":20.1}'
curl -s -o /dev/null -w "conflict   -> %{http_code}\n" -X POST "$BASE/windows/$WID/readings" \
  -H 'Content-Type: application/json' \
  -d '{"reading_id":"r1","revision":1,"timestamp":"2026-09-01T08:00:00Z","temperature":99.9}'
curl -s -o /dev/null -w "outside    -> %{http_code}\n" -X POST "$BASE/windows/$WID/readings" \
  -H 'Content-Type: application/json' \
  -d '{"reading_id":"rx","revision":1,"timestamp":"2026-09-01T07:59:00Z","temperature":20.0}'

echo "== 4. 查看审定结果（连续超限 3600s > 900s => fail）=="
curl -s "$BASE/windows/$WID/evaluation" | j

echo "== 5. 签发第 1 版 =="
curl -s -X POST "$BASE/windows/$WID/certificates" | python3 -c 'import sys,json;d=json.load(sys.stdin);print("v%d %s"%(d["version"],d["verdict"]))'

echo "== 6. r3/r4 提交更高修订号 2（改回正常温度），旧修订号拒绝 =="
curl -s -X POST "$BASE/windows/$WID/readings" -H 'Content-Type: application/json' \
  -d '{"reading_id":"r3","revision":2,"timestamp":"2026-09-01T09:00:00Z","temperature":20.0}' | j
curl -s -X POST "$BASE/windows/$WID/readings" -H 'Content-Type: application/json' \
  -d '{"reading_id":"r4","revision":2,"timestamp":"2026-09-01T09:30:00Z","temperature":20.0}' | j
curl -s -o /dev/null -w "stale rev1 -> %{http_code}\n" -X POST "$BASE/windows/$WID/readings" \
  -H 'Content-Type: application/json' \
  -d '{"reading_id":"r3","revision":1,"timestamp":"2026-09-01T09:00:00Z","temperature":22.8}'

echo "== 7. 重算 / 再次签发 v2，旧版本快照保留 =="
curl -s "$BASE/windows/$WID/evaluation" | python3 -c 'import sys,json;d=json.load(sys.stdin);print("now:",d["verdict"],"longest_exceeded=",d["longest_continuous_exceeded_seconds"])'
curl -s -X POST "$BASE/windows/$WID/certificates" | python3 -c 'import sys,json;d=json.load(sys.stdin);print("v%d %s"%(d["version"],d["verdict"]))'
curl -s "$BASE/windows/$WID/certificates" | python3 -c 'import sys,json;print([(c["version"],c["verdict"]) for c in json.load(sys.stdin)])'
curl -s "$BASE/windows/$WID/certificates/1" | python3 -c 'import sys,json;d=json.load(sys.stdin);r=[x for x in d["readings"] if x["reading_id"]=="r3"][0];print("v1 snapshot r3:",r["revision"],r["temperature"],"verdict:",d["verdict"])'
