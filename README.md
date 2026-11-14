# DeepScope

每一步可恢复、可回放、可归因的深度研究 Agent Runtime（开发中）。

当前处于 **Spike 阶段**：用裸 async 函数验证两大卖点——引用质量（≥70%）与中断恢复可行性，**尚未引入** LangGraph / FastAPI / 数据库 / 前端。

## Spike 快速开始

```bash
# 1. 安装依赖（Python 3.12+）
python -m venv .venv
.venv/Scripts/activate        # Windows；Linux/Mac: source .venv/bin/activate
pip install -r requirements.txt

# 2. 配置 Key
cp .env.example .env          # 填入 DEEPSEEK_API_KEY / TAVILY_API_KEY / BOCHA_API_KEY（后两者至少一个）

# 3. 验证四个 API（重点看 Tavily 在当前网络下是否可用）
python check_apis.py

# 4. 跑第一题
python spike.py --qid q1

# 5. 跑全部 5 题
python spike.py --all

# 中断恢复演示：跑到 fetch/extract 阶段按 Ctrl+C，重跑同一条命令，
# 自动从下一个未完成的 step 继续（已完成的 plan/search 不重复）
```

## 输出结构（out/<qid>/）

| 文件 | 内容 |
|---|---|
| `report.md` | 带 [e#] 证据级引用的研究报告 |
| `evidence.json` | 全部证据（id / claim / 原文逐字 quote / 来源） |
| `meta.json` | 成本（人民币，区分 cache hit/miss）、token、耗时、每步明细、引用准确率 |
| `checkpoint.json` | 中断恢复点（每步完成原子写入） |
| `steps/01_plan.json` … `06_verify.json` | 每步输入/输出/耗时/成本，步骤可回放 |

## 流水线

`plan → search → fetch → extract → write → verify`，六个步骤全部裸 async 函数实现，详见 `spike.py`。

## 决策日志

所有技术权衡记录在 [DECISIONS.md](DECISIONS.md)。
