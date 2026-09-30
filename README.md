# 博物馆展柜温度暴露审定服务

基于 **Python / FastAPI / SQLite** 的展柜温度暴露审定（certification）服务：
创建审定窗口、提交温度读数、按规则切分区段并给出结论，可对结论进行版本化签发。

- 在线接口文档：`http://localhost:8000/docs`（Swagger UI）、`/redoc`
- 健康检查：`GET /health`

---

## 1. 快速启动（Docker Compose 一键启动）

前置要求：已安装 Docker 与 Docker Compose 插件。

```bash
docker compose up --build -d        # 构建镜像、初始化依赖并后台启动
curl http://localhost:8000/health   # {"status":"ok"}
docker compose logs -f              # 查看日志
docker compose down                 # 停止（数据保留在命名卷 museum-data 中）
```

容器启动时自动安装依赖并在 `/data/museum.db` 初始化 SQLite 数据库（含建表）。
数据保存在 Docker 命名卷 `museum-data` 中，容器重建不丢失。

### 本地直接运行（不使用 Docker）

需要 Python 3.11+：

```bash
python -m pip install -r requirements.txt
export DATABASE_PATH=./data/museum.db          # 可选，默认 /data/museum.db
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 运行测试

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

---

## 2. 配置

| 配置项 | 环境变量 | 默认值 | 说明 |
|---|---|---|---|
| 监听地址 | uvicorn 参数 `--host` | `0.0.0.0` | Docker 中固定为全部网卡 |
| 监听端口 | uvicorn 参数 `--port` / compose `ports` | `8000` | 宿主机端口可在 `docker-compose.yml` 修改左侧端口 |
| 数据库路径 | `DATABASE_PATH` | `/data/museum.db` | SQLite 文件路径，父目录不存在会自动创建 |

服务地址（默认）：

- 基础地址：`http://localhost:8000`
- Swagger UI：`http://localhost:8000/docs`
- ReDoc：`http://localhost:8000/redoc`
- OpenAPI JSON：`http://localhost:8000/openapi.json`

时间格式统一为 ISO 8601（建议带时区，如 `2026-09-01T08:00:00Z`）；
不带时区的时间戳按 UTC 解释。时长字段单位均为秒。

---

## 3. 审定规则

### 3.1 区段覆盖语义

读数按时间戳升序排列，**每条读数只覆盖到下一条读数为止**，该段温度取本段起点读数：

1. 相邻两条读数间隔 **严格大于** 窗口的 `max_sampling_interval_seconds`
   → 这两个读数之间整段记为 **未知（unknown）**；
2. 窗口起点早于第一条读数 → 起点到首条读数为 **未知**（边界无读数）；
3. 最后一条读数到窗口终点：差值 **严格大于** 最大采样间隔 → **未知**，
   否则由最后一条读数覆盖（恰等于间隔上限仍算覆盖）；
4. 窗口内没有任何读数 → 整个窗口为未知；
5. 被覆盖的区段：温度落在允许区间 `[temp_min, temp_max]`（含边界）为 **正常**，
   否则为 **超限**；
6. 时间相邻、状态相同的区段在输出中合并；零长度区段忽略。

### 3.2 结论判定（优先级从高到低）

- 最长**连续**超限时长 **严格大于** `max_continuous_exceed_seconds`
  → `fail`（不合格，未知区段会打断连续性）；
- 否则，只要存在未知区段 → `inconclusive`（待定）；
- 其余 → `pass`（合格）。

### 3.3 读数提交：幂等、冲突与修订

- 相同 `reading_id` + `revision` + 相同内容（时间戳、温度）重复提交：**幂等**，
  返回 `200` 与 `status=idempotent`，不重复落库；
- 相同 `reading_id` + `revision` 但内容不一致：**冲突报错** `409 conflicting_revision`；
- 同一 `reading_id` 提交**更高修订号**：旧修订行标记为已替换（保留历史、不参与计算），
  之后所有审定与签发基于新值**重算**，返回 `status=replaced`；
- 已存在更高修订号时再提交旧修订号：**拒绝** `409 stale_revision`；
- 时间戳落在窗口之外（边界等于窗口端点是允许的）→ `422 outside_window`；
- 不同读数落在同一时刻 → `422 duplicate_timestamp`；
- 结束时间不晚于起始时间、温度下限高于上限、非有限温度等 → `422`。

### 3.4 并发与签发版本

- SQLite 运行在 WAL 模式；所有写事务（读数提交、证书签发）以 `BEGIN IMMEDIATE`
  开启，进入事务即取锁。因此**签发与修订串行化**：一次签发在事务内看到的读数
  必然是此前某一次完整提交的状态（同一已提交状态），不会读到修订到一半的数据。
- 签发结果（结论、全部区段、当时生效读数）作为**不可变 JSON 快照**保存。
  已签发的旧版本永远保持原结论不变；读数被修订后再次签发会生成**递增的新版本**。
  即使数据没有变化，再次签发也会产生新版本（每次签发动作都可审计）。

---

## 4. HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| POST | `/windows` | 创建审定窗口（无效窗口返回 422） |
| GET | `/windows` | 窗口列表 |
| GET | `/windows/{window_id}` | 窗口详情 |
| POST | `/windows/{window_id}/readings` | 提交读数（201 新提交/替换，200 幂等，409/422 拒收） |
| GET | `/windows/{window_id}/readings?include_superseded=false` | 读数列表（默认仅生效读数） |
| GET | `/windows/{window_id}/evaluation` | 查看当前审定结果（不签发） |
| POST | `/windows/{window_id}/certificates` | 签发证书（生成下一版本快照） |
| GET | `/windows/{window_id}/certificates` | 证书版本列表 |
| GET | `/windows/{window_id}/certificates/{version}` | 获取指定版本证书（原始快照） |

### 错误响应格式

```json
{ "error": { "code": "conflicting_revision", "message": "…", "details": {} } }
```

| HTTP | code | 含义 |
|---|---|---|
| 404 | `not_found` | 窗口 / 证书版本不存在 |
| 409 | `conflicting_revision` | 相同 ID + 修订号内容冲突 |
| 409 | `stale_revision` | 提交的修订号低于当前生效修订号 |
| 422 | `validation_error` | 请求体字段校验失败 |
| 422 | `invalid_window` | 起止时间 / 温度区间非法 |
| 422 | `outside_window` | 读数时间戳在窗口外 |
| 422 | `duplicate_timestamp` | 同刻不同读数 |

---

## 5. 调用示例（curl）

以下示例脚本也保存在 [`examples/curl-examples.sh`](examples/curl-examples.sh)，
可在服务启动后直接执行：`bash examples/curl-examples.sh`。

### 5.1 创建审定窗口

窗口：2026-09-01 08:00–12:00（UTC）；允许温度 18–22℃；最长连续超限 900 秒；
最大采样间隔 1800 秒。

```bash
curl -s -X POST http://localhost:8000/windows \
  -H 'Content-Type: application/json' \
  -d '{
    "start": "2026-09-01T08:00:00Z",
    "end":   "2026-09-01T12:00:00Z",
    "temp_min": 18.0,
    "temp_max": 22.0,
    "max_continuous_exceed_seconds": 900,
    "max_sampling_interval_seconds": 1800
  }'
```

### 5.2 提交读数

```bash
curl -s -X POST http://localhost:8000/windows/$WID/readings \
  -H 'Content-Type: application/json' \
  -d '{"reading_id":"r1","revision":1,"timestamp":"2026-09-01T08:00:00Z","temperature":20.1}'
# -> 201 {"status":"accepted", ...}

# 完全相同的请求再发一次
# -> 200 {"status":"idempotent", ...}

# 相同 ID + 修订号但温度改成别的值
# -> 409 conflicting_revision

# 窗口外时间戳
# -> 422 outside_window
```

### 5.3 查看审定结果

```bash
curl -s http://localhost:8000/windows/$WID/evaluation
```

响应（示例）：

```json
{
  "window_id": "…",
  "verdict": "fail",
  "segments": [
    { "start": "2026-09-01T08:00:00Z", "end": "2026-09-01T09:00:00Z",
      "duration_seconds": 3600.0, "status": "normal", "temperatures": [20.1, 20.4] },
    { "start": "2026-09-01T09:00:00Z", "end": "2026-09-01T10:00:00Z",
      "duration_seconds": 3600.0, "status": "exceeded", "temperatures": [22.8, 23.1] },
    { "start": "2026-09-01T10:00:00Z", "end": "2026-09-01T12:00:00Z",
      "duration_seconds": 7200.0, "status": "unknown", "temperatures": null }
  ],
  "normal_seconds": 3600.0,
  "exceeded_seconds": 3600.0,
  "unknown_seconds": 7200.0,
  "window_seconds": 14400.0,
  "longest_continuous_exceeded_seconds": 3600.0
}
```

### 5.4 签发、修订、再次签发

```bash
# 签发第 1 版（fail 也会如实签发并留档）
curl -s -X POST http://localhost:8000/windows/$WID/certificates
# -> {"version":1,"verdict":"fail", ...}

# 用更高修订号替换 r1（修正错误读数），审定结果立即按新值重算
curl -s -X POST http://localhost:8000/windows/$WID/readings \
  -H 'Content-Type: application/json' \
  -d '{"reading_id":"r1","revision":2,"timestamp":"2026-09-01T08:00:00Z","temperature":20.0}'
# -> 201 {"status":"replaced","replaced_revision":1, ...}

# 再提交旧修订号 -> 409 stale_revision

# 重新签发 -> 新版本；旧版本快照保持不变
curl -s -X POST http://localhost:8000/windows/$WID/certificates          # version 2
curl -s http://localhost:8000/windows/$WID/certificates                  # 版本列表
curl -s http://localhost:8000/windows/$WID/certificates/1                # v1 原始快照
```

### 5.5 Python 调用片段

```python
import httpx

BASE = "http://localhost:8000"

wid = httpx.post(f"{BASE}/windows", json={
    "start": "2026-09-01T08:00:00Z", "end": "2026-09-01T12:00:00Z",
    "temp_min": 18.0, "temp_max": 22.0,
    "max_continuous_exceed_seconds": 900,
    "max_sampling_interval_seconds": 1800,
}).json()["id"]

httpx.post(f"{BASE}/windows/{wid}/readings", json={
    "reading_id": "r1", "revision": 1,
    "timestamp": "2026-09-01T08:00:00Z", "temperature": 20.1,
})

print(httpx.get(f"{BASE}/windows/{wid}/evaluation").json()["verdict"])
cert = httpx.post(f"{BASE}/windows/{wid}/certificates").json()
print(cert["version"], cert["verdict"])
```

---

## 6. 目录结构

```
app/
  main.py          # FastAPI 路由与异常处理
  schemas.py       # Pydantic 请求 / 响应模型
  service.py       # 业务逻辑（窗口、幂等/修订、审定、签发版本）
  evaluation.py    # 纯函数审定引擎（区段切分、合并、判定）
  db.py            # SQLite 连接、建表、IMMEDIATE 事务
  config.py        # 环境变量配置
  timeutil.py      # 时间戳转换（naive 按 UTC）
tests/             # pytest：引擎单测 + API 集成测试
examples/          # curl 调用示例脚本
Dockerfile
docker-compose.yml
requirements.txt / requirements-dev.txt
```
