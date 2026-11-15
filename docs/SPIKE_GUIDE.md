# SPIKE_GUIDE.md — 大白话读懂 spike.py

> 写给正在补 Python 基础的你。不要求逐行读懂代码，只要求能讲清楚「它在干嘛、为什么这么干」。
> 对照着 `spike.py` 看，函数名可以在文件里搜索定位。

---

## 1. 整体：一个六道工序的流水线

想象一个工厂流水线，原料是「一个研究问题」，产品是「一份带引用的报告」：

```
问题 → [plan 规划] → [search 搜索] → [fetch 抓网页] → [extract 摘证据] → [write 写报告] → [verify 审稿] → 报告+评分
```

每一道工序做完，就把「半成品 + 状态」存档（checkpoint）。如果工厂停电（进程被 kill），重新开机时从存档处继续，前面做完的不重做。

**这就是整个项目的种子**：后面 W2-W3 要做的 Runtime，就是把这个「存到文件」升级成「存到数据库 + 能被网页端控制暂停/恢复」。

---

## 2. 六道工序各自在干嘛

| 工序 | 代码函数 | 一句话 | 比喻 |
|---|---|---|---|
| plan | `step_plan` | 让 LLM 把问题拆成子问题 + 5 个搜索词 | 先列采购清单再出门 |
| search | `step_search` | 每个搜索词调 Tavily，失败换博查 | 去两家超市，一家关门去另一家 |
| fetch | `step_fetch` / `fetch_page` | 把搜索结果里的网页正文抓下来；Jina 失败换 trafilatura，再失败用搜索返回的摘要兜底 | 三层备用方案 |
| extract | `step_extract` | 让 LLM 从每页正文里摘「论断 + 原文逐字引用」 | 抄卡片：论点 + 原句 |
| write | `step_write` | 让 LLM 只用这些卡片写报告，每句话挂卡片编号 [e3] | 只许用现有卡片答题 |
| verify | `step_verify` | 另一个 LLM 当审稿人，逐条检查「引用是否真支持那句话」 | 出版前校对 |

## 3. 三个关键机制（面试必讲）

### 3.1 checkpoint：中断恢复（`Workspace.save_checkpoint` / `load_checkpoint`）

每道工序完成 → 把整个状态写进 `out/<qid>/checkpoint.json` → 下次启动先读它，已完成的工序跳过。

**为什么重要**：这不是「没写完可以继续」的小功能，而是 W3 验收点「kill 进程后从上次节点继续」的原型。你在 Spike 里已经真实触发过一次：全量重跑被意外终止，重跑后自动续跑成功。

### 3.2 机械闸门：防伪造引用（`quote_in_page`）

extract 摘到的「原文引用」必须逐字出现在抓取的网页正文里，否则丢弃并记入 `dropped_quotes.json`。

**为什么重要**：LLM 会编造「看起来像原文」的英文（Spike 抓到 40 条）。这是引用可信度的第一道防线——**机器字符串比对，零成本，绝对客观**。第二道防线才是 LLM judge（判断语义是否支持），因为 judge 是 AI，判断不了「这句话原文里到底存不存在」。

### 3.3 双层校验：为什么准确率可信（`step_verify`）

| 层 | 手段 | 查什么 | 局限 |
|---|---|---|---|
| 机械层 | 字符串子串匹配 | quote 是否真实存在于原文 | 查不了「是否支持」 |
| 语义层 | LLM judge | 引用是否支持论断 | 查不了「原文是否存在」 |

两层合起来才是完整定义。Spike 修过的两个 bug 恰好各对应一层：伪造 quote（机械层漏洞）→ 加闸门；judge 输入缺 title（语义层漏洞）→ 传完整证据。

### 3.4 成本核算：每分钱都记账（`LLMProvider.chat`）

每次调 LLM，从返回的 `usage` 里读 token 数，乘以单价（`.env` 里可配）换成人民币。**区分 cache hit / miss**：输入内容命中缓存便宜 4 倍。

**为什么重要**：这就是「可归因」的雏形——每一步花了多少钱都能查到（`out/<qid>/meta.json`）。W3 预算感知、W5 Java 配额结算都从这里长出来。

---

## 4. 对照 Python 知识点（补基础时回来看）

| spike.py 里的东西 | 对应知识点 | 在哪个函数 |
|---|---|---|
| `async def` / `await` / `asyncio.gather` | 异步编程：等网页、等 LLM 时不干等，同时干别的 | `step_fetch`、`step_extract` 里的并发 |
| `@dataclass` | 数据类：省去手写 `__init__` | `SpikeState`、`StepRecord`、`LLMResult` |
| `list[dict]`、`-> str` | 类型注解：给变量标类型，IDE 能提示 | 全文件 |
| `try / except` | 异常处理：出错不崩，走备用方案 | `fetch_page`、`chat` 的重试 |
| `json.loads / dumps` | JSON 解析与序列化 | `extract_json`、`atomic_write_json` |
| `re.search / findall` | 正则表达式：找 `[e3]` 标记、校验 quote | `quote_in_page`、`_chunks_by_marks` |
| 函数作为参数（`PIPELINE` 列表） | 高阶函数：把「工序」当对象排队执行 | `run_pipeline` |

补基础时，每学完一个知识点就回来看对应的函数——**Spike 就是你自己的习题集**。

---

## 5. 一个任务从提交到出分的完整路径

```
python spike.py --qid q1
  └─ main() 读 questions.json
      └─ run_pipeline(q)
          ├─ 读 checkpoint：有 → 跳过已完成工序
          ├─ 依序执行 6 个 step，每个完成后：
          │    ├─ save_step → out/q1/steps/02_search.json（步骤可回放）
          │    └─ save_checkpoint → out/q1/checkpoint.json（中断恢复）
          └─ finalize → report.md + evidence.json + meta.json（三件套）
```

产出文件：

| 文件 | 给谁看 |
|---|---|
| `report.md` | 人看：带 [e#] 的报告 |
| `evidence.json` | 机器和人看：每条证据的论断/原文/来源 |
| `meta.json` | 机器看：成本、token、耗时、准确率 |
| `steps/*.json` | 审计看：每一步的输入输出（可回放） |
| `checkpoint.json` | 恢复机制自己看 |

---

## 6. 三个「如果面试官问」的预备答案

**Q: 为什么 Spike 不用 LangGraph？**
先用最少的代码验证内容质量（引用能不能 ≥70%）；如果连裸函数都做不到，框架也救不了。W2 才引入 LangGraph，且核心节点全部自己写，只用它的状态管理和 checkpoint。

**Q: 引用准确率怎么定义？**
strict = 完全支撑的引用数 / 总引用数。机械闸门先保证 quote 真实存在，judge 再判语义支持。judge 会偏松，所以又加了「校验覆盖率」指标和人工抽查校准——q3 抽查时发现 judge 输入缺 title，修掉后 +9.4 个百分点。

**Q: 中断恢复验证过吗？**
验证过，而且是意外验证的——全量重跑被 kill，重跑自动从断点继续。Spike 粒度是「工序级」（最多重跑当前工序），W3 的目标是节点级 + 幂等。
