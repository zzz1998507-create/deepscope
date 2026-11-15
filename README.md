# DeepScope

**每一步可恢复、可回放、可归因的深度研究 Agent Runtime。**

输入一个研究问题，Agent 在预算内自主完成多轮检索、阅读、反思，产出带证据级引用的报告；全过程可暂停、恢复、取消、回放，进程被 kill 后能从上次节点继续。

当前处于 **W1 Spike 阶段**：用裸 async 函数验证「引用质量 ≥70%」和「中断恢复可行」两个卖点，尚未引入 LangGraph / FastAPI / 数据库 / 前端。

---

## 架构概览

```text
React 控制台（3 页）
   │ REST / SSE
   ▼
Java 控制面（Spring Boot 3 + Java 21 虚拟线程）
   │  任务生命周期 / 鉴权 / 幂等 / 限流 / SSE 扇出与重放 / 审批 / 配额
   ▼
Python Agent 执行面（FastAPI + LangGraph）
   │  plan → search → fetch → extract → write → verify
   ▼
PostgreSQL 16 + pgvector
```

- **Python 是执行真相源**：LangGraph checkpointer 保存运行时状态
- **Java 是控制面真相源**：任务状态、配额、审批、事件服务副本
- 详见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

## 技术栈

| 层 | 主要技术 |
|---|---|
| 前端 | React + Vite + TypeScript + Ant Design 5 |
| Java 控制面 | Java 21 + Spring Boot 3.x + Spring MVC（虚拟线程）+ JPA + Flyway |
| Python 执行面 | Python 3.12 + FastAPI + LangGraph + SQLAlchemy/asyncpg |
| 存储 | PostgreSQL 16 + pgvector；Redis 可选 |
| 部署 | Docker Compose |
| AI 服务 | DeepSeek / SiliconFlow BGE-M3 / Tavily / 博查 / Jina / trafilatura |

## 当前状态

- Spike 六步流水线已跑通：`plan → search → fetch → extract → write → verify`
- q1 第三轮：54 条证据、9 个来源、引用准确率 **83.3% / 98.9%**、成本 ¥0.24、耗时 282 秒
- 已实现：抓取正文校验、伪造 quote 机械闸门、verify 按引用数分批、校验覆盖率指标
- 待完成：q1 人工抽查、q2-q5、整体 Go/No-Go

## 快速开始（Spike）

```bash
# 1. 安装依赖（Python 3.12+）
python -m venv .venv
.venv/Scripts/activate        # Windows；Linux/Mac: source .venv/bin/activate
pip install -r requirements.txt

# 2. 配置 Key
cp .env.example .env          # 填 DEEPSEEK_API_KEY；TAVILY_API_KEY / BOCHA_API_KEY 至少一个

# 3. 验证 API（重点看 Tavily 在当前网络下是否可用）
python check_apis.py

# 4. 跑第一题
python spike.py --qid q1

# 5. 跑全部 5 题
python spike.py --all

# 中断恢复演示：跑到 fetch/extract 阶段按 Ctrl+C，重跑同一条命令，
# 自动从下一个未完成的 step 继续（已完成的 plan/search 不重复）
```

## 输出结构（`out/<qid>/`）

| 文件 | 内容 |
|---|---|
| `report.md` | 带 `[e#]` 证据级引用的研究报告 |
| `evidence.json` | 证据（id / claim / 原文逐字 quote / 来源） |
| `meta.json` | 成本（区分 cache hit/miss）、token、耗时、每步明细、引用准确率 |
| `checkpoint.json` | 中断恢复点（每步完成原子写入） |
| `steps/01_plan.json` … `06_verify.json` | 每步输入/输出/耗时/成本，步骤可回放 |
| `dropped_quotes.json` | 机械校验未通过（伪造/改写）的候选引用 |

## 仓库结构

```text
deepscope/
├─ docs/          # 架构设计
├─ spike.py       # W1 原型，W2 迁入 agent/
├─ check_apis.py
├─ questions.json
├─ DECISIONS.md   # 技术决策日志
└─ README.md
```

## 文档

- [架构设计](docs/ARCHITECTURE.md)
- [决策日志](DECISIONS.md)

## 里程碑

W1 Spike → W2 状态机 + 证据库 → W3 反思循环 + 四个验收点 → W4 前端 3 页 + 人工介入 + 评测基线 → W5 Java BFF v1 + 评测集 → W6 并发硬化 + 压测 → W7 打磨 + demo。
