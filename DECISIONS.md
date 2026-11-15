# DECISIONS.md — DeepScope 决策日志

> 记录格式：日期 / 问题 / 候选方案 / 选择 / 代价 / 后续验证。每天至少一条。
> 面试叙事要求：每个"我实现了"都要能升级成"我权衡了"——本文件就是原材料。

---

## 2026-11-15

### 决策：Java BFF 从"W6 可选"改为"W5 必做"（目标修正）
- **背景**：用户的真实求职目标是 Java 高并发控制面 + Python Agent 执行面；早期摘要中"Java BFF 可选、时间不够可砍"的假设不再成立。
- **选择**：Java 控制面负责认证、任务生命周期、幂等/限流、SSE 长连接扇出与 `Last-Event-ID` 重放、k6 压测；Python 执行面负责 Agent Runtime、checkpoint（执行真相源）、评测与可观测性。两者用 `task_id + seq` 对账，Java 不复刻 Agent 内部状态。
- **代价**：预计增加 20-25 小时；压缩前端交互与评测集规模（维持 15-20 题），Redis 队列降为可选。
- **后续验证**：W5 末 BFF v1 跑通；W6 用 mock agent 模式压测：500 并发 SSE、100 次/秒提交、取消/重试幂等、断线重放无缺口。

### 问题：verify 按字符切块导致输出溢出，99 处引用只校验了 34 处（已修复）
- **现象**：批次 1 包含 65 处引用标记，两次调用输出都撞 4096 token 上限，只返回 1 条判定；最终 accuracy 0.882 仅基于 34/99 处引用，代表性不足。
- **根因**：judge 的判定条数与输出 token 大致成正比，"按字符切块"不等于"按判定数切块"；且当前模型可能包含推理 token，进一步挤占输出预算。
- **修复**：`_chunks_by_marks()` 按引用标记数切块（每批 ≤15 处，实测 7 批）；verify `max_tokens` 提到 6144；判定数不足 50% 时自动拆半重试；新增 `verification_coverage` 指标，低于 70% 打警告。
- **代价**：verify 从 2 次调用变为 7 次，成本预计增加 ¥0.02-0.04。
- **后续验证**：重跑 q1，校验覆盖率应 ≥90%，每批 10-15 条判定。

### 决策：extract 输出上限从 2048 提到 4096
- **问题**：上轮至少一次 extract 输出正好 2048 token，存在 JSON 被截断、claims 静默丢失的风险。
- **选择**：上限提到 4096；同时机械闸门保证截断导致的坏 quote 不会进入证据库。
- **代价**：仅在模型实际用满时才多花钱。

### 决策：前端选 React + Ant Design 5（Vue 3 评估后放弃）
- **问题**：用户前端薄弱，React 和 Vue 都不熟，选哪个？
- **候选方案**：A. React 18/19 + Ant Design 5；B. Vue 3 + Element Plus。
- **选择**：A。两个都不熟时，"熟悉度"不构成理由；AntD 对 Timeline / Drawer / Table / Form 的覆盖、AI 代码生成质量、大厂后台生态更优，且原计划就是它。
- **代价**：需要 4-6 小时补 React 基础（组件、useState、useEffect、SSE）。
- **后续验证**：W3 末写一个最小"列表 + SSE 日志"demo；若明显不适应，W4 前仍可换 Vue，W4 后不换。

### 决策：Java 控制面技术栈
- **选择**：Java 21 LTS + Spring Boot 3.x + Spring MVC（虚拟线程 + `SseEmitter`）+ Spring Security/JWT + Spring Data JPA + Flyway + Bucket4j + WebClient + Actuator/Micrometer + Maven。
- **不选**：WebFlux（与 JPA 阻塞模型别扭、复杂度高）、Spring Cloud/Kafka（7 周内不需要）、MyBatis（当前 SQL 简单，JPA 省代码；若后续更熟 MyBatis 可替换，但禁止两套混用）。
- **代价**：虚拟线程下的 SSE 连接管理、背压需要自己验证；W6 才能拿到压测数据。
- **后续验证**：W5 BFF v1 跑通；W6 用 k6 + mock agent 压测。

### 决策：Python 工具链
- **选择**：Python 3.12（W2 起）+ FastAPI/Uvicorn + LangGraph + `AsyncPostgresSaver` + SQLAlchemy 2 async/asyncpg + Alembic + Ruff + Pyright + pytest/pytest-asyncio/respx。
- **代价**：Spike 当前运行在 3.14，W2 需要新建 3.12 venv 并迁移依赖。
- **后续验证**：W2 用 3.12 跑通 LangGraph checkpoint 与断点恢复。

### 决策：开发工具分工固定为 PyCharm / Cursor / IDEA
- **选择**：Python 侧用 **PyCharm**，前端用 **Cursor**，Java 侧用 **IntelliJ IDEA**；三者不混用。
- **理由**：PyCharm 的 Python 异步调试、pytest、解释器管理最成熟；Cursor 的 AI 补全最适合生成和修改 React 组件；IDEA 的 Spring Boot / Maven / 数据库工具最顺。
- **代价**：三个 IDE 会增加切换成本；用统一 Git 工作流和同一仓库结构缓解。PyCharm Community 不含数据库/HTTP 工具，数据库看 DBeaver，接口调试用 Bruno/Insomnia。
- **后续验证**：W2 用 PyCharm 跑通 LangGraph checkpoint；W4 用 Cursor 写前端；W5 用 IDEA 跑 BFF。

### 决策：W2-W5 集成设计的五条修正
1. **JavaBusinessClient Protocol + LocalMock**：W2-W3 用本地 mock 跑通 context/library/usage/approval，W5 只替换 HTTP 实现，状态机不动。
2. **审批用 `interrupt()` + `Command(resume=...)`**：`create_approval` 非阻塞返回 approval_id，节点随后 interrupt；恢复时节点会重跑，interrupt 前的副作用必须幂等。
3. **usage 幂等键 `(task_id, call_id)`**：`call_id` 对应每次真实 provider 请求（UUID），重试也换新；`logical_call_id` 用于聚合重试。
4. **事件 per-task seq**：Python 生成 seq；Java `task_events` 持久化并扇出；Python 保留 outbox 防 Java 宕机丢事件；前端用 `Last-Event-ID` 重放。
5. **状态对账**：Python 执行态优先；Java 未 ack 的 cancel/resume 优先；`last_event_seq` 以 Python 为准；`budget_used` 双口径（Java 结算、Python 硬停），差异告警不阻塞。
- **后续验证**：W3 做 kill/restore 与审批暂停/恢复测试；W5 用 contract tests 同时跑 mock 和真实 Java。

### 问题：deepseek-flash 推理 token 挤占输出预算，报告 40 字截断、accuracy 塌方（已定位）
- **现象**：`completion_tokens=8192` 用满但报告只有 40 字（"# ChatGPT Deep Research、Perplexity"），verify 无判定可用。
- **根因**：deepseek-flash 为推理型模型，输出预算大部分被思考过程占用，正文没空间；与 prompt 无关。
- **处理**：改用非推理模型 `deepseek-chat`；write 后检测截断（末尾无句末标点则告警），meta.json 记录 `report_truncated`。
- **教训**：指标塌方先查模型行为再改 prompt；推理模型的 token 计量和输出预算需要单独策略。
- **后续验证**：W2 Provider 层对推理模型单独设 output 上限和截断检测。

### 决策：q5 证据来源缺失（25 结果只抓到 2 页）→ Tavily raw_content 兜底
- **问题**：q5 搜索 25 条结果只成功抓取 2 个页面，ChatGPT Deep Research 完全无来源，strict 69.6% 卡在 Go 线下。
- **修复**：Tavily 搜索请求加 `include_raw_content=true`；抓取失败时用搜索返回的正文兜底（channel=tavily_raw），机械闸门同样对其校验。
- **效果**：q5 页面 2→15、证据 12→50、strict 69.6%→88.9%。
- **后续验证**：W2 搜索 Provider 默认开启 raw_content；观测 Tavily 免费额度消耗速度。

### 决策：write 阶段证据选择与写作约束
- **问题**：write 把多个来源要点合并成一句长句（一句挂 4-5 个引用），judge 逐条判定必然 partial，strict 被系统性压低。
- **修复**：`_select_evidence` 控制规模（≤30 条、每来源 ≤4 条保证多样性）；prompt 要求一句一事实、每句 ≤2 个引用、全文 2500-3500 字、不再输出证据列表。
- **代价**：报告信息密度降低，深度略减；换取引用可核验性。
- **后续验证**：q1-q5 全量重跑后 strict 是否整体回升。

### 结果：W1 Spike 全量重跑（deepseek-chat + 最新代码，2026-11-15）
| qid | strict | lenient | 证据 | 来源 | 耗时 | 成本 | 校验覆盖 | 丢弃伪造 |
|---|---|---|---|---|---|---|---|---|
| q1 | 86.2% | 100% | 65 | 14 | 74s | ¥0.097 | 100% | 19 |
| q2 | 75.0% | 100% | 67 | 12 | 63s | ¥0.094 | 91.2% | 4 |
| q3 | 74.1% | 100% | 58 | 11 | 73s | ¥0.094 | 87.1% | 8 |
| q4 | 90.4% | 96.2% | 55 | 10 | 77s | ¥0.089 | 85.2% | 5 |
| q5 | 88.9% | 100% | 50 | 9 | 67s | ¥0.094 | 96.4% | 4 |
- 五题合计 354s / ¥0.47；最低 strict 74.1% > 70% Go 线。
- 中断恢复现场验证：全量重跑被 kill 后，重跑（无 --fresh）自动 skip 已完成步骤续跑成功。
- 超时降级现场验证：trafilatura TimeoutError → tavily_raw 兜底多次生效。

---

## 2026-11-14

### 决策：Spike 阶段刻意不用 LangGraph，用裸 async 函数验证内容质量
- **问题**：Spike 要回答"引用质量 ≥70% 是否可达"和"中断恢复是否可行"，用 LangGraph 是否更好？
- **候选方案**：A. 直接上 LangGraph；B. 裸 async 函数 + 手写 checkpoint；C. 用 DeepAgents/DSH。
- **选择**：B。LangGraph 的状态机在 W2 再引入。
- **代价**：Spike 的 checkpoint 机制与 W2 正式状态机不直接复用，需重写一遍编排层。
- **后续验证**：W2 引入 LangGraph 时对比：手写 checkpoint 的经验（原子写、step 幂等边界）哪些能平移。

### 决策：成本单价默认值用 DeepSeek 官方 V3.2 定价（可 .env 覆盖）
- **问题**：成本核算的单价从哪来？
- **候选方案**：A. 硬编码；B. .env 可配置 + 默认值；C. 跑完由 API 返回价格字段。
- **选择**：B，默认 hit=¥0.5/M、miss=¥2/M、output=¥8/M；meta.json 里记录实际使用的单价，保证成本可复核。
- **代价**：价格变动（如 V4 调价）时默认值失真，需人工核对。
- **后续验证**：跑完 q1 后到 DeepSeek 官网核对当前单价并更新 .env。

### 决策：搜索降级顺序 Tavily → 博查，降级不中断流水线
- **问题**：搜索 Provider 不可用（网络/Key/额度）时怎么办？
- **候选方案**：A. 直接失败停止；B. 自动降级备用 Provider 并记录；C. 并发双查取并集。
- **选择**：B。降级信息写入该 query 的 step 记录（可归因）。
- **代价**：博查的中文结果覆盖面与 Tavily 不同，可能影响后 3 题的英文来源质量。
- **后续验证**：跑 q4/q5（英文内容为主）时抽查来源多样性，若明显劣化再评估。

### 决策：Spike 版引用准确率定义（LLM judge 逐引用判定）
- **问题**：Spike 没有评测集，Go/No-Go 的"引用准确率 ≥70%"怎么算？
- **候选方案**：A. 人工逐条核对（最准，慢）；B. LLM judge 逐处 [e#] 判定 supported/partial/unsupported；C. 只数引用数不判真伪（无意义）。
- **选择**：B，报两个数：strict = supported/total，lenient = (supported+partial)/total；Go/No-Go 看 strict。
- **代价**：LLM judge 自身有偏差（对自己生成的报告可能偏松），Spike 结果只是上界估计。
- **后续验证**：用户人工抽查 q1 报告与 judge 判定的一致性；W4 评测集落地后换成正式口径。

### 决策：Spike 的"中断恢复"用 step 级 checkpoint 验证
- **问题**：Spike 阶段怎么验证"kill 后从上次节点继续"？
- **候选方案**：A. step 完成后原子写 checkpoint，重启跳过已完成 step；B. 每步内做子任务级快照；C. 只落盘不实现续跑。
- **选择**：A。失败/被 kill 时最多重跑当前 step（search/fetch 会重复调用外部 API，Spike 阶段接受）。
- **代价**：不能做到"半路中断不重复一次搜索"（那是 W3 验收点，需要 step 内部幂等）。
- **后续验证**：跑 q1 时故意在 fetch 阶段 Ctrl+C，重跑确认从 fetch 开始、plan/search 不重复。

### 问题：q1 首跑 11 页只产出 9 条证据、全部来自 2 个来源（2026-11-14 修复）
- **现象**：抓取日志全部是 `trafilatura`；检查 checkpoint 发现 9/11 页正文以 `<!DOCTYPE html>` 开头，证据 quote 实际来自 meta description。
- **根因**：`trafilatura.fetch_url()` 只下载 HTML 不抽取正文，需再接 `trafilatura.extract()`。首版 fallback 用错了 API，等于把 `<head>` 喂给 LLM。
- **修复**：`fetch_url` + `extract(output_format="markdown", favor_precision=True)`；并对抓回内容做 HTML 前缀校验，防止把错误页当正文。
- **教训**：流水线每个 stage 边界要有"产物形态 sanity check"（正文不该以 `<` 开头）；verify 报 1.0 准确率时先怀疑输入数据，而不是庆祝。
- **后续验证**：重跑 q1 --fresh，证据来源数应从 2 升到 5+，引用准确率重新计算。

### 问题：extract 产出伪造 quote（e13/e33），LLM judge 无法发现（2026-11-14 修复）
- **现象**：65 条证据中 2 条 quote 在全部 14 个抓取页面和搜索摘要中都找不到（前 60 字符零命中），是模型编造的"看起来像原文"的英文。
- **根因**：prompt 要求逐字复制，但模型仍会改写/编造；而 verify 的 LLM judge 只看 evidence.json 里的 quote 和 claim，两者自洽即判 supported，**judge 判断不了 quote 是否真实存在**。
- **修复**：extract 后加机械闸门 `quote_in_page()`——HTML unescape + 空白归一 + 按省略号分段做子串匹配，不通过直接丢弃并写入 `dropped_quotes.json`。
- **代价**：会误杀少量做了合法格式调整的 quote（宁严勿松，Spike 阶段可接受）。
- **教训**：引用校验必须两层——机械层查"是否存在于原文"，语义层查"是否支持论断"，缺一不可。
- **后续验证**：重跑 q1，确认 dropped_quotes 捕获伪造项、evidence 全部逐字命中。

### 问题：verify 单次校验 148 处引用导致输出溢出、accuracy 不可用（2026-11-14 修复）
- **现象**：模型只返回了单个 check 对象，`checks` 为空，accuracy 显示 None/None。
- **根因**：148 处引用一次塞进 4096 token 输出，模型输出退化。
- **修复**：按 4000 字符分批校验；每批只发送该批实际引用的证据（省 token）；`_normalize_checks` 兼容三种输出形态；覆盖度评估拆成独立调用。
- **代价**：verify 从 1 次调用变成 3-4 次，成本略增。
- **后续验证**：重跑 q1，accuracy 应正常产出，且批次日志能看到每批判定数。

### 决策：Jina Reader 做 run 级熔断
- **问题**：当前网络访问 `r.jina.ai` 20s 超时，首跑每个 URL 都白等 15s（fetch 27.9s 大部分是空等）。
- **选择**：同一 run 内 Jina 首次失败后置 `jina_ok=False`，后续 URL 直接走 trafilatura。
- **代价**：网络恢复后同一 run 内不会重试 Jina；下一 run 自动恢复。
- **后续验证**：观察 fetch 耗时是否从 ~28s 降到 ~10s。

### 决策：LLM 直连 httpx，不引 openai SDK
- **问题**：DeepSeek 是 OpenAI 兼容接口，用什么客户端？
- **候选方案**：A. openai SDK；B. httpx 直连。
- **选择**：B。usage 的 cache hit/miss 拆分字段完全透明，成本核算直接基于原始 usage；少一层依赖，也符合"核心自写"的叙事。
- **代价**：后续要支持流式（W4 SSE）时需手写 SSE 解析。
- **后续验证**：W4 做 SSE 时评估是否值得引入 SDK 的流式封装。
