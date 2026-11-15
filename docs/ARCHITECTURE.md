# DeepScope 架构设计

> 状态：设计稿 v0.1 · 2026-11-15 · W1 Spike 阶段
> 本文是技术选型、模块边界和接口契约的事实来源；实现进度以 README 和 DECISIONS.md 为准。

---

## 1. 项目定位

**一句话**：DeepScope 是一个「每一步可恢复、可回放、可归因」的深度研究 Agent Runtime。

**电梯陈述**：输入一个研究问题，Agent 在预算内自主完成多轮检索、阅读、反思，产出带证据级引用的报告；全过程可暂停、恢复、取消、回放，进程被 kill 后能从上次节点继续。

**要证明的两件事**：

1. Agent 架构能力——Runtime 是核心，不是 prompt demo
2. AI 产品全栈交付能力——前端控制台 + Java 控制面 + Python 执行面 + 评测 + 部署

**定位**：求职信号，不是产品。

### 明确不做

- 浏览器 Agent、多 Agent 协作、跨会话长期记忆
- 通用 Agent 平台、AIOps、Text2SQL 等已排除方向
- Kafka、多租户、复杂计费、完整用户体系
- 超过 3 个前端页面
- fork 开源项目当起点

---

## 2. 四个卖点

| 卖点 | 内容 | 验证方式 |
|---|---|---|
| **可恢复 Runtime**（最高优先级） | checkpoint、断点续跑、幂等取消/重试、执行隔离 | kill 进程后重启，从上次节点继续 |
| **评测驱动** | 引用准确率、关键点覆盖、成本/延迟、回归 | W4 起 5-10 题基线，W5 扩到 15-20 题 |
| **证据级引用** | 证据单独存库，论断绑定 source span，自动检测「引用不支持论断」 | 机械逐字校验 + LLM judge 双层 |
| **预算感知** | token / 时间 / 搜索次数预算，Reflect 代码级硬停 | 轮数上限 + 预算计数器 |

---

## 3. 系统架构

```text
React 控制台（3 页）
   │ REST / SSE
   ▼
Java 控制面（Spring Boot 3 + Java 21 虚拟线程）
   │  任务生命周期 / 鉴权 / 幂等 / 限流 / SSE 扇出与重放 / 审批 / 配额
   │
   │ ① POST /internal/tasks（202）
   │ ② 工具回调：context / library / usage / approval / artifact
   ▼
Python Agent 执行面（FastAPI + LangGraph）
   │  plan → search → fetch → extract → write → verify
   │  checkpoint（执行真相源）/ 事件 / 证据库 / 评测
   ▼
PostgreSQL 16 + pgvector
   （Redis 可选：限流 / 队列 / 事件传输）
```

**职责边界**：

- **Python 是执行真相源**：LangGraph checkpointer 保存运行时状态
- **Java 是控制面真相源**：任务状态、配额、审批、事件服务副本
- 两边用 `task_id + seq` 对账；Java 不复刻 Agent 内部状态

---

## 4. 三层职责

### 4.1 前端 `web/`

- 创建页：研究问题、预算、提交
- 轨迹页：SSE 实时事件、暂停 / 补充指令 / 恢复、取消
- 报告页：报告渲染、`[e#]` 引用点击 → 证据抽屉
- 不直接访问 Python；W4 可临时直连，W5 切到 Java

### 4.2 Java 控制面 `server/`

- 认证鉴权（JWT）与用户配额
- 任务生命周期状态机（submitted / running / paused / cancelling / cancelled / completed / failed）
- 幂等键、限流、并发控制
- SSE 扇出、事件持久化、`Last-Event-ID` 重放
- 人工审批记录与通知
- 报告元数据与 ACL
- 对账：Java 启动后拉取非终态任务状态

### 4.3 Python 执行面 `agent/`

- LangGraph 状态机与自定义节点
- checkpoint（`AsyncPostgresSaver`）与断点续跑
- 工具层：Web Search / Fetch / Java 业务接口
- 证据抽取、双层引用校验
- 预算硬停
- 事件产生（per-task seq）与 outbox
- 评测集与评测脚本

### 4.4 存储

| 数据 | 归属 | 说明 |
|---|---|---|
| 任务控制状态、幂等键、配额、审批 | Java | PostgreSQL |
| 事件服务副本 | Java | `task_events`，用于扇出和重放 |
| checkpoint、证据、报告、usage | Python | PostgreSQL + pgvector |
| 事件源/outbox | Python | 防 Java 宕机丢事件 |
| 向量 | Python | pgvector，BGE-M3 1024 维 |

---

## 5. 技术栈

### 前端

React 18/19 + Vite + TypeScript + Ant Design 5 + React Router + TanStack Query + `@microsoft/fetch-event-source` + `react-markdown` / `remark-gfm`。

不用 Redux、不用 Next.js。

### Java 控制面

Java 21 LTS + Spring Boot 3.x + Spring MVC（虚拟线程 + `SseEmitter`）+ Spring Security + JWT + Spring Data JPA + Hibernate + Flyway + PostgreSQL Driver + WebClient + Bucket4j + Actuator/Micrometer + Jakarta Validation + Maven。

测试：JUnit 5 + MockMvc + Testcontainers + WireMock；压测：k6。

不用 WebFlux、Spring Cloud、Kafka。

### Python 执行面

Python 3.12 + FastAPI + Uvicorn + LangGraph + `langgraph-checkpoint-postgres` + Pydantic v2 + pydantic-settings + httpx + SQLAlchemy 2 async + asyncpg + Alembic + pgvector + `sse-starlette` + structlog/loguru。

测试与质量：pytest + pytest-asyncio + respx + Ruff + Pyright。

不用 Celery（W2 先用 asyncio 后台任务）、不用 LangChain legacy agent、不用 DeepAgents。

### 基础设施

PostgreSQL 16 + pgvector；Redis 7 可选；Docker + Docker Compose；Nginx；Git + GitHub；GitHub Actions（W5+）。

### 外部 AI 服务

DeepSeek（LLM 主力）、SiliconFlow BGE-M3（Embedding）、Tavily（搜索主力）、博查（搜索降级）、Jina Reader（可选）、trafilatura（本地正文抽取兜底）。

---

## 6. 接口契约

### 6.1 公开 API（Java → 前端）

```text
POST   /api/auth/login
POST   /api/research/tasks                 # Idempotency-Key
GET    /api/research/tasks/{id}
POST   /api/research/tasks/{id}/cancel     # 幂等
POST   /api/research/tasks/{id}/resume     # 补充指令后继续
GET    /api/research/tasks/{id}/events     # SSE + Last-Event-ID
GET    /api/research/tasks/{id}/report
POST   /api/research/tasks/{id}/approvals/{aid}
GET    /api/capabilities
```

### 6.2 Java → Python（内部）

```text
POST   /internal/tasks                     # 按 task_id 幂等，返回 202
GET    /internal/tasks/{id}
GET    /internal/tasks/{id}/status         # 对账用
GET    /internal/tasks/{id}/events         # SSE + seq
POST   /internal/tasks/{id}/cancel
POST   /internal/tasks/{id}/resume
GET    /internal/tasks/{id}/report
```

`GET /internal/tasks/{id}/status` 返回：

```json
{
  "status": "running|paused|completed|failed|cancelled",
  "current_node": "extract",
  "last_event_seq": 42,
  "budget_used": {"tokens": 12000, "search_calls": 6, "cost_rmb": 0.08}
}
```

### 6.3 Python → Java（工具回调，服务令牌鉴权）

```text
GET    /internal/research/tasks/{id}/context
GET    /internal/research/users/{uid}/library
POST   /internal/research/tasks/{id}/usage
POST   /internal/research/tasks/{id}/approvals
POST   /internal/research/tasks/{id}/artifacts
```

鉴权：Java 创建任务时签发短生命周期、绑定 `task_id` 的服务令牌；Python 不保存用户 JWT 到模块级全局变量。

### 6.4 事件 schema

```json
{
  "task_id": "uuid",
  "seq": 42,
  "type": "step.finished",
  "ts": "2026-11-15T08:00:00Z",
  "payload": {}
}
```

事件类型：

`task.created`、`step.started`、`step.finished`、`token.delta`、`usage.updated`、`checkpoint.saved`、`approval.required`、`task.paused`、`task.resumed`、`task.cancelled`、`task.completed`、`task.failed`。

规则：

- `seq` 由 Python 生成，per-task 单调递增；Java 只存储和扇出，绝不重编号
- Java `task_events` 唯一索引 `(task_id, seq)`，插入 `ON CONFLICT DO NOTHING`
- 写事件与更新 `last_event_seq` 在同一事务；或由 `max(seq)` 派生
- Python 保留事件源/outbox，Java 事件表是可重放的 serving copy
- 前端断线重连带 `Last-Event-ID`，Java 补推 `seq > last_event_id`

### 6.5 真相源与对账

**规则**：

- 执行状态（running / paused / completed / failed）以 Python 为准
- 控制命令（cancel / resume）以 Java 为准，直到 Python ack
- `last_event_seq` 永远以 Python 为准，Java 补齐缺失事件
- `budget_used`：Java 用于配额/结算，Python 用于任务内硬停；差异超阈值告警，不阻塞恢复

**状态冲突优先级**：

| 场景 | 判定 |
|---|---|
| Java 有未确认的 cancel/resume | Java 优先，直到 Python ack |
| Python terminal，Java 非 terminal | 接受 Python 终态 |
| Python paused（审批），Java 无 cancel | paused 优先 |
| Java cancelled，Python completed | 看 cancel 时间：完成前 → cancelled；否则 completed |
| 双方 terminal 且冲突 | cancelled > failed > completed，并告警 |

---

## 7. 关键设计决策

1. **LangGraph 核心节点自己写**，不用 DeepAgents、不 fork DeerFlow / gpt-researcher。
2. **Java 层必做**：W5 BFF v1，W6 并发硬化；不是「W6 可选」。
3. **审批用 `interrupt()` + `Command(resume=...)`**：`create_approval` 非阻塞，节点先创建审批再 interrupt；恢复时节点会重跑，副作用必须幂等。
4. **usage 幂等键 `(task_id, call_id)`**：`call_id` 每次真实 provider 请求生成 UUID，重试也换新；`logical_call_id` 用于聚合。
5. **W2-W3 用 `JavaBusinessClient` Protocol + LocalMock**，W5 只替换实现，状态机不动。
6. **引用双层校验**：`quote_in_page()` 机械逐字校验防伪造 + LLM judge 查语义支持。
7. **预算双保险**：Java 管用户配额/结算，Python 管单任务代码级硬停。
8. **前端 React + Ant Design 5**：Vue 3 + Element Plus 评估过但放弃——两个框架都不熟时，AntD 组件覆盖 + AI 代码生成质量 + 大厂后台生态更优。
9. **Java 用 Spring MVC + 虚拟线程 + `SseEmitter`**，不上 WebFlux；JPA 而非 MyBatis；Maven。
10. **checkpoint 唯一化**：W2 起 LangGraph `AsyncPostgresSaver` 是执行真相源；Spike 的 `checkpoint.json` 退役为审计记录，不保留两套恢复机制。

---

## 8. 仓库结构

```text
deepscope/
├─ web/                    # Vite + React + TS + AntD
├─ server/                 # Spring Boot（Java 21）
├─ agent/                  # FastAPI + LangGraph
│  ├─ deepscope/
│  │  ├─ api/              # 内部 REST + SSE
│  │  ├─ runtime/          # 状态机、checkpoint、预算
│  │  ├─ tools/            # web / java_business
│  │  ├─ providers/        # LLM / Embedding / Search
│  │  └─ eval/             # 评测集与脚本
│  └─ tests/
├─ contracts/              # OpenAPI + 事件 schema
├─ deploy/                 # docker-compose.yml、nginx.conf
├─ docs/                   # 架构、交接文档
├─ spike.py                # W1 原型，W2 迁入 agent/
├─ check_apis.py
├─ questions.json
├─ DECISIONS.md
└─ README.md
```

---

## 9. 里程碑

| 周 | Python 侧 | Java 侧 |
|---|---|---|
| W1 | Spike 收尾 + Go/No-Go | — |
| W2 | LangGraph 状态机 + 工具层 + 证据库 | — |
| W3 | 反思循环 + 报告 + 断点续跑 + 四个验收点 | — |
| W4 | 前端 3 页 + SSE + 人工介入 + 评测基线 | API 契约设计 |
| W5 | 评测集 15-20 题 + 首轮优化 | BFF v1：JWT、任务 API、幂等、SSE 代理、限流 |
| W6 | 回归 + 优化 | 虚拟线程、背压、重连重放、k6 压测；可选 Redis 队列 |
| W7 | 打磨 + README + demo + 简历 | 联调收尾 |

### Runtime 四个验收点

| 验收点 | 时间 | 验证方式 |
|---|---|---|
| 工具循环检测 | W3 | 故意触发重复搜索，N 次后强制换策略或停止 |
| 上下文超限处理 | W3 | 超窗口历史摘要压缩；Writer 按需检索证据 |
| 状态丢失/中断恢复 | W3 后半 / W4 初 | kill 进程后重启，从上次节点继续 |
| 幂等取消/重试 | W4 初 | 同一任务取消两次、重试两次，副作用不重复 |

---

## 10. 当前状态与风险

**已完成**：

- Spike 六步流水线（裸 async）
- 三道质量机制：抓取正文校验、伪造 quote 机械闸门、verify 按引用数分批
- q1 第三轮：54 条证据、9 个来源、引用准确率 83.3% / 98.9%、¥0.24、282 秒
- GitHub private 仓库、SSH over 443

**待完成 / 风险**：

- q1 人工抽查、q2-q5、整体 Go/No-Go（尚未完成）
- Python 3.12 迁移：W2 引入 LangGraph / asyncpg / pgvector 时执行
- Java BFF 预计增加 20-25 小时；压缩前端交互和评测集规模，但不砍最小评测基线
- LLM judge 偏差：需人工抽查校准；验收时同时看 accuracy 和 verification coverage
- 「高并发」用 mock agent 压测控制面，不宣称真实 LLM 流量；数字写清测试条件
- Redis 队列、内部资料库为 W6 可选加分项

---

## 11. 变更记录

| 日期 | 变更 |
|---|---|
| 2026-11-15 | v0.1：项目定位、三层架构、技术栈、接口契约、关键决策、里程碑 |
