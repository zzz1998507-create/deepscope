# DECISIONS.md — DeepScope 决策日志

> 记录格式：日期 / 问题 / 候选方案 / 选择 / 代价 / 后续验证。每天至少一条。
> 面试叙事要求：每个"我实现了"都要能升级成"我权衡了"——本文件就是原材料。

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
