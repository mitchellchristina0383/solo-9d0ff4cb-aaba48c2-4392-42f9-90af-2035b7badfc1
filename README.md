# Dependency Alert API

值班工程师面对一串服务故障告警时，需要先处理真正的**上游根因故障**，同时保留尚未恢复的
下游告警但予以抑制。本服务用 **FastAPI + SQLite（WAL）** 实现：

- 整版提交服务依赖图（`A` 依赖 `B`，则 `B` 是 `A` 的上游）；
- 带**每服务递增正整数序号**上报故障 / 恢复，支持幂等重试、冲突拒绝、迟到不回退；
- 查询时动态计算**需处理故障**与**被抑制故障**及其上游根因；
- 替换依赖图时按服务名保留状态，恢复后下游自动重新归类，无需重报；
- 所有写操作走序列化事务，并发上报 / 换版 / 查询不会混合不同已提交状态。

## 运行

### Docker Compose（推荐）

```bash
docker compose up --build
# API:   http://localhost:8000
# 文档:  http://localhost:8000/docs   （Swagger UI，可直接操作）
# 健康:  http://localhost:8000/health
```

SQLite 数据文件保存在命名卷 `alert-data`（容器内 `/data/alerts.db`）。

### 本地直接运行

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
ALERT_DB_PATH=./data/alerts.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/graph` | 整版替换依赖图。校验失败（环路 / 未知引用 / 重名 / 重边 / 自依赖）返回 `422` 并**保留旧版**；成功换版会使所有进行中的升级批次失效 |
| `GET` | `/graph` | 读取当前已提交图（含内部修订号 `revision`） |
| `POST` | `/reports` | 上报故障 / 恢复 |
| `GET` | `/alerts` | 查询告警分类（需处理 / 被抑制 / 根因 / 路径） |
| `POST` | `/upgrade-batches` | 从当前图选择服务，创建升级批次（每服务一个步骤，原子写入） |
| `GET` | `/upgrade-batches` | 列出全部批次（含已失效）及进度 |
| `GET` | `/upgrade-batches/{id}` | 批次详情；已失效批次的已完成步骤仍可查看 |
| `POST` | `/upgrade-batches/{id}/steps/{service}/claim` | 领取升级步骤（满足前置条件时返回领取令牌） |
| `POST` | `/upgrade-batches/{id}/steps/{service}/complete` | 凭领取令牌回报成败 |
| `POST` | `/admin/reset` | 清空全部数据（测试 / 运维辅助） |
| `GET` | `/health` | 健康检查 |

### 提交依赖图

```json
POST /graph
{
  "version": "v1",
  "services": [
    {"name": "A", "dependencies": ["B", "C"]},
    {"name": "B", "dependencies": ["D"]},
    {"name": "C", "dependencies": ["D"]},
    {"name": "D", "dependencies": []}
  ]
}
```

`dependencies` 中列出的是**上游**服务；所有被引用服务必须在同一次提交中出现，
整图必须是 DAG（Kahn 算法检环）。每次成功提交 `revision` 递增。

### 上报故障 / 恢复

```json
POST /reports
{"service": "B", "seq": 7, "status": "fault"}
```

序号 `seq` 为**每个服务各自维护的严格递增正整数**，规则：

| 情况 | 结果 |
| --- | --- |
| `seq > last_seq` | 应用新状态 |
| `seq == last_seq` 且内容相同 | 幂等重试，`applied=false, duplicate=true`，状态不变 |
| `seq == last_seq` 但内容不同（fault/recover 互斥） | `409 conflict`，状态不变 |
| `seq < last_seq`（迟到上报） | `409 conflict`，**绝不回退状态** |
| 服务不在当前图中 | `404 unknown_service` |
| `seq <= 0` | `422`（请求体校验） |

序号允许有空洞（如 1 之后直接 3）。每次上报（含被拒绝的）都写入 `events` 审计表。

### 查询告警

```json
GET /alerts
{
  "graph_revision": 1,
  "graph_version": "v1",
  "services": ["A", "B", "C", "D"],
  "actionable_faults": [{"service": "D", "last_seq": 1}],
  "suppressed_faults": [
    {
      "service": "B",
      "last_seq": 1,
      "root_causes": ["D"],
      "paths": [["B", "D"]]
    }
  ],
  "down_services": ["B", "D"],
  "unreported_services": ["C"],
  "healthy_services": []
}
```

## 抑制规则（核心语义）

1. 只有当前仍处于故障（down）状态的服务才会出现在 `actionable_faults` 或
   `suppressed_faults` 中，两类互斥且并集恰为全部 down 服务。
2. **根因**：故障且没有任何故障上游的服务。根因永远是需处理故障。
3. 一个非根因故障被抑制，当且仅当存在一条到达某根因的依赖路径，且**路径上每个节点都
   处于故障中**。
4. 健康节点或**从未上报**的节点会截断路径：其下游即使故障也不能经由该路径被抑制；
   若所有路径都被截断，该故障仍归入需处理。
5. 分类是查询时根据当前图 + 当前状态**动态计算**的。服务恢复、依赖图换版后立即重新
   归类，不需要下游重新上报。

## 换版与状态保留

- 服务状态（up/down、最后序号）独立于图存储，按服务名为主键。换版后同名服务的状态与
  序号历史原样保留；被移除服务的状态行不删除但不会出现在查询中，将来重新加入时恢复可见。
- 新图中首次出现的服务归入 `unreported_services`。
- 换版成功时，所有 `active` 升级批次在同一事务内被标记为 `invalidated`（见下节）。

## 升级批次（发布值班）

发布值班员按依赖顺序升级选定服务：先创建批次，再逐个**领取**步骤、升级、**回报成败**。

```json
POST /upgrade-batches
{"services": ["D", "B", "A"]}          // 必须全部来自当前图；未知 / 重复服务整体拒绝
```

```json
POST /upgrade-batches/1/steps/B/claim  // 无请求体
→ 200 {"claim_token": "…", "claim_epoch": 1, "health_snapshot": {"B": 1, "D": 1}}
```

```json
POST /upgrade-batches/1/steps/B/complete
{"claim_token": "…", "result": "success"}     // 或 "failure"
```

**领取前置条件**（全部满足才可领取，否则 `409` 并指明原因）：

1. 批次处于 `active`（其图修订号未被换版取代）；
2. 本服务与**全部传递上游**（含批次外上游）都已上报且当前健康（`up`）；
3. 批次内所有上游步骤均已 `succeeded`。

**并发与回执语义**：

- 同一步骤的并发领取在串行事务中裁决，**只有一个有效领取**，其余 `409 already_claimed`。
- 领取时对"本服务 + 全部上游"记录健康快照 `{service: last_seq}`。领取后任一相关服务
  接受了更高序号的上报（即使随后恢复），旧领取的成功回执会被拒绝
  （`409 health_snapshot_invalid`），该轮作废回到 `failed`，**不能放行下游**；需重新领取。
- `failure` 回报使步骤回到 `failed`，可立即重新领取；每轮领取换新令牌并递增
  `claim_epoch`，**迟到的旧轮回执**（令牌不符）一律 `409 stale_claim_token`，不会覆盖新一轮。
- 图换版成功 → 旧批次 `invalidated`，领取 / 回报均 `409 batch_invalidated`；
  `GET` 详情不受影响，已完成步骤仍可查看。换版校验失败（保留旧图）时批次不受影响。
- 批次创建在单个事务内完成校验与写入：失败不留半份批次；回报只更新单个步骤行，
  不产生虚假完成。

## 并发模型

- SQLite 开启 WAL；每个写事务以 `BEGIN IMMEDIATE` 开始，写者全局串行化，遇
  `SQLITE_BUSY` 短时重试。
- 读事务持有单一已提交快照，因此换版与查询并发时，每次 `/alerts` 看到的节点集、边与
  修订号必然属于同一个已提交版本，不会出现混合状态。
- 每个请求使用独立的线程级连接，请求结束即关闭。

## 自测

`tests/selftest.py` 仅依赖标准库，对运行中的服务做端到端校验：

```bash
python tests/selftest.py                      # 默认 http://127.0.0.1:8000
BASE_URL=http://localhost:8000 python tests/selftest.py
# 容器内：docker compose exec alert-api python tests/selftest.py
```

覆盖：图校验与原子替换、序号幂等 / 冲突 / 迟到语义、全故障路径抑制与健康 / 未上报截断、
恢复即时重分类、换版状态保留，同序号并发重试、同序号异内容竞争、多序号乱序竞争、
并发换版、换版与查询交错，以及升级批次的创建原子性、领取前置条件、依赖顺序、
并发领取唯一获胜、领取后故障 / 恢复使成功回执作废、失败后重领取与迟到回执拒绝、
换图失效与历史可读等场景（共 131 项断言）。

## 目录结构

```
app/
  main.py             FastAPI 路由与异常映射
  database.py         SQLite 连接、schema、事务管理
  graph_service.py    图校验、整版替换（同事务失效旧批次）、告警分类
  report_service.py   上报序号语义
  upgrade_service.py  升级批次：创建、领取、回报、失效
  schemas.py          Pydantic 模型
  errors.py           领域错误
tests/selftest.py     端到端自测（标准库）
Dockerfile, docker-compose.yml, requirements.txt
```
