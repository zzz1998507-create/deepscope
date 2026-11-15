"""
DeepScope Spike — 纯 async 函数验证两大卖点：引用质量 + 中断恢复。

流程（不用 LangGraph，刻意裸写，先验证内容质量）：
    plan → search → fetch → extract → write → verify

Spike 阶段的 Runtime 雏形（服务于 W2-W4 的正式状态机）：
- 每完成一个 step 立即原子写 checkpoint（out/<qid>/checkpoint.json）
  → kill 进程后重跑同一条命令，自动从下一个未完成的 step 继续，不重复已完成步骤；
- 每个 step 的输入/输出/耗时/成本落盘 out/<qid>/steps/<nn>_<name>.json
  → "步骤可回放"验收点的 Spike 版证据；
- LLM 调用内建超时 + 指数退避重试（网络错误 / 5xx），验证一次超时重试链路；
- 成本按 DeepSeek 定价换算人民币，区分 cache hit / miss token，单价可配（.env）。

用法：
    python spike.py --qid q1          # 跑单题（有 checkpoint 自动续跑）
    python spike.py --all             # 顺序跑全部
    python spike.py --qid q1 --fresh  # 忽略 checkpoint 重跑
    python check_apis.py              # 先验证四个 Key 可用（含 Tavily 网络检查）
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import re
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).parent
OUT = ROOT / "out"
QUESTIONS_FILE = ROOT / "questions.json"

# ---------------- 配置 ----------------

PRICES = {
    "input_cache_hit": float(os.getenv("PRICE_LLM_INPUT_CACHE_HIT", "0.5")),
    "input_cache_miss": float(os.getenv("PRICE_LLM_INPUT_CACHE_MISS", "2.0")),
    "output": float(os.getenv("PRICE_LLM_OUTPUT", "8.0")),
}  # 元 / 百万 token

MAX_SEARCH_QUERIES = int(os.getenv("SPIKE_MAX_SEARCH_QUERIES", "8"))
MAX_PAGES = int(os.getenv("SPIKE_MAX_PAGES", "15"))
MAX_PAGE_CHARS = int(os.getenv("SPIKE_MAX_PAGE_CHARS", "12000"))

LLM_TIMEOUT_S = 120.0
LLM_RETRIES = 2           # 网络错误/5xx 时重试次数（指数退避 2s/4s）
JINA_TIMEOUT_S = 15.0     # 超时后降级 trafilatura
TRAFILATURA_TIMEOUT_S = 20.0
FETCH_CONCURRENCY = 5
EXTRACT_CONCURRENCY = 3
SEARCH_CONCURRENCY = 3

# ---------------- 工具函数 ----------------

def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def extract_json(text: str) -> Any:
    """从 LLM 输出中宽容地提取 JSON：剥围栏、取首个完整 JSON 值。失败抛 ValueError。"""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    decoder = json.JSONDecoder()
    for i in range(len(text)):
        ch = text[i]
        if ch in "{[":
            try:
                obj, _ = decoder.raw_decode(text[i:])
                return obj
            except json.JSONDecodeError:
                continue
    raise ValueError(f"未找到合法 JSON，输出前 300 字符：{text[:300]!r}")


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _norm_text(t: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(t)).strip().lower()


def quote_in_page(quote: str, content: str) -> bool:
    """机械校验：quote 的每一段（按省略号切分）必须是页面正文的逐字子串。

    这是防"模型伪造 quote"的硬闸门，不花任何 API 成本。
    LLM judge 只能判断 quote 是否支持论断，判断不了 quote 是否真实存在于原文。
    """
    norm_content = _norm_text(content)
    parts = [p for p in re.split(r"[…]|\.{3,}", quote) if len(p.strip()) >= 12]
    if not parts:
        return _norm_text(quote) in norm_content
    return all(_norm_text(p) in norm_content for p in parts)


# ---------------- LLM Provider ----------------

@dataclass
class LLMResult:
    text: str
    input_cache_hit: int = 0
    input_cache_miss: int = 0
    output_tokens: int = 0
    cost_rmb: float = 0.0
    latency_s: float = 0.0

    @property
    def tokens(self) -> dict:
        return {
            "input_cache_hit": self.input_cache_hit,
            "input_cache_miss": self.input_cache_miss,
            "output": self.output_tokens,
        }


class LLMProvider:
    """OpenAI 兼容的极简客户端（httpx 直连，不引 openai SDK，usage 字段完全透明）。

    重试策略：网络错误 / 5xx 重试 LLM_RETRIES 次（指数退避）；4xx 不重试直接抛。
    这构成 Spike 的"超时重试"验证链路之一（另一条是 Jina 超时降级 trafilatura）。
    """

    def __init__(self, name: str, base_url: str, api_key: str, model: str,
                 client: httpx.AsyncClient):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.client = client

    async def chat(
        self,
        messages: list[dict],
        *,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        json_mode: bool = False,
    ) -> LLMResult:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        last_err: Exception | None = None
        for attempt in range(LLM_RETRIES + 1):
            started = time.monotonic()
            try:
                resp = await self.client.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                    timeout=httpx.Timeout(LLM_TIMEOUT_S),
                )
                if resp.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"服务端 5xx: {resp.status_code}", request=resp.request, response=resp)
                if resp.status_code != 200:
                    body = resp.text[:500]
                    raise RuntimeError(
                        f"API 返回 {resp.status_code}（4xx 不重试）: {body}")
                data = resp.json()
                usage = data.get("usage", {})
                hit = usage.get("prompt_cache_hit_tokens", 0)
                miss = usage.get("prompt_cache_miss_tokens")
                if miss is None:  # 未返回 cache 拆分时，全部按未命中计（保守）
                    miss = usage.get("prompt_tokens", 0)
                out_tok = usage.get("completion_tokens", 0)
                result = LLMResult(
                    text=data["choices"][0]["message"]["content"] or "",
                    input_cache_hit=hit,
                    input_cache_miss=miss,
                    output_tokens=out_tok,
                    latency_s=time.monotonic() - started,
                )
                result.cost_rmb = (
                    hit / 1e6 * PRICES["input_cache_hit"]
                    + miss / 1e6 * PRICES["input_cache_miss"]
                    + out_tok / 1e6 * PRICES["output"]
                )
                log(f"  [{self.name}] {result.latency_s:.1f}s "
                    f"hit={hit} miss={miss} out={out_tok} ¥{result.cost_rmb:.4f}")
                return result
            except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as e:
                last_err = e
                if attempt < LLM_RETRIES:
                    wait = 2 ** attempt * 2
                    log(f"  [{self.name}] 调用失败（{type(e).__name__}），{wait}s 后重试 "
                        f"({attempt + 1}/{LLM_RETRIES})")
                    await asyncio.sleep(wait)
        raise RuntimeError(f"[{self.name}] 重试 {LLM_RETRIES} 次后仍失败: {last_err}")

    async def chat_json(self, messages: list[dict], **kw: Any) -> Any:
        """要求 JSON 输出并解析；解析失败时带错误反馈重试一次（防格式漂移）。"""
        for attempt in range(2):
            result = await self.chat(messages, json_mode=True, **kw)
            try:
                return extract_json(result.text), result
            except ValueError as e:
                if attempt == 0:
                    messages = messages + [
                        {"role": "assistant", "content": result.text},
                        {"role": "user", "content":
                            f"你的输出不是合法 JSON：{e}。请重新只输出 JSON，不要任何其他文字。"},
                    ]
                    continue
                raise RuntimeError(f"JSON 解析两次失败: {e}") from e
        raise AssertionError("unreachable")


# ---------------- 搜索 ----------------

@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str
    raw_content: str = ""


async def tavily_search(client: httpx.AsyncClient, query: str, max_results: int = 5) -> list[SearchHit]:
    key = os.getenv("TAVILY_API_KEY", "")
    if not key:
        raise RuntimeError("未配置 TAVILY_API_KEY")
    resp = await client.post(
        "https://api.tavily.com/search",
        json={"api_key": key, "query": query, "max_results": max_results,
              "search_depth": "basic", "include_raw_content": True},
        timeout=httpx.Timeout(30.0),
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Tavily 返回 {resp.status_code}: {resp.text[:300]}")
    return [SearchHit(r.get("title", ""), r.get("url", ""), r.get("content", ""),
                      r.get("raw_content") or "")
            for r in resp.json().get("results", []) if r.get("url")]


async def bocha_search(client: httpx.AsyncClient, query: str, max_results: int = 5) -> list[SearchHit]:
    key = os.getenv("BOCHA_API_KEY", "")
    if not key:
        raise RuntimeError("未配置 BOCHA_API_KEY")
    resp = await client.post(
        "https://api.bochaai.com/v1/web-search",
        headers={"Authorization": f"Bearer {key}"},
        json={"query": query, "freshness": "noLimit", "summary": True, "count": max_results},
        timeout=httpx.Timeout(20.0),
    )
    if resp.status_code != 200:
        raise RuntimeError(f"博查返回 {resp.status_code}: {resp.text[:300]}")
    data = resp.json().get("data", {})
    pages = data.get("webPages") or data.get("webpages") or {}
    hits = []
    for item in pages.get("value", []) or []:
        url = item.get("url", "")
        if url:
            hits.append(SearchHit(item.get("name", ""), url,
                                  item.get("snippet") or item.get("summary", "")))
    return hits[:max_results]


async def search_with_fallback(client: httpx.AsyncClient, query: str,
                               max_results: int = 5) -> tuple[str, list[SearchHit], str | None]:
    """Tavily 优先，任何异常降级博查；两者都失败则返回空并记录错误。"""
    providers = [("tavily", tavily_search), ("bocha", bocha_search)]
    errors = []
    for name, fn in providers:
        try:
            hits = await fn(client, query, max_results)
            if hits:
                return name, hits, None
            errors.append(f"{name}: 0 条结果")
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__}: {e}")
    return "none", [], "; ".join(errors)


# ---------------- 网页正文 ----------------

def _trafilatura_extract(url: str) -> str | None:
    """下载 HTML 并抽取正文。

    注意：fetch_url 只负责下载，返回原始 HTML；必须再调 extract 才能去掉
    导航/脚本/标签。直接返回 fetch_url 的结果会把 <head> 喂给 LLM。
    """
    import trafilatura
    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        return None
    return trafilatura.extract(
        downloaded,
        output_format="markdown",
        include_comments=False,
        include_tables=False,
        favor_precision=True,
    )


async def fetch_page(client: httpx.AsyncClient, url: str,
                     jina_state: dict) -> tuple[str | None, str]:
    """Jina Reader 优先，trafilatura 兜底。

    jina_state 是本次运行共享的熔断状态：Jina 第一次失败后，后续页面直接跳过，
    避免每个 URL 都白等一次超时（国内网络下 Jina 常不可达）。
    """
    if jina_state.get("jina_ok", True):
        jina_key = os.getenv("JINA_API_KEY", "")
        headers = {"Authorization": f"Bearer {jina_key}"} if jina_key else {}
        try:
            resp = await client.get(f"https://r.jina.ai/{url}", headers=headers,
                                    timeout=httpx.Timeout(JINA_TIMEOUT_S))
            body = resp.text
            # Jina 失败时会返回 HTML 错误页，不能当正文
            if resp.status_code == 200 and len(body) >= 200 and not body.lstrip().startswith("<"):
                return body[:MAX_PAGE_CHARS], "jina"
            jina_state["jina_ok"] = False
            log(f"  ! Jina 返回异常（HTTP {resp.status_code}），本次运行后续改用 trafilatura")
        except Exception as e:
            jina_state["jina_ok"] = False
            log(f"  ! Jina 不可达（{type(e).__name__}），本次运行后续改用 trafilatura")

    try:
        text = await asyncio.wait_for(
            asyncio.to_thread(_trafilatura_extract, url), timeout=TRAFILATURA_TIMEOUT_S)
        if text and len(text) >= 200:
            return text[:MAX_PAGE_CHARS], "trafilatura"
    except Exception as e:
        log(f"  ! trafilatura 失败 {url[:60]}: {type(e).__name__}: {e}")
    return None, "failed"


# ---------------- 状态与 Checkpoint ----------------

@dataclass
class StepRecord:
    name: str
    started_at: str = ""
    finished_at: str = ""
    duration_s: float = 0.0
    cost_rmb: float = 0.0
    tokens: dict = field(default_factory=dict)
    input_summary: dict = field(default_factory=dict)
    output_summary: dict = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict:
        return self.__dict__


@dataclass
class SpikeState:
    qid: str
    question: str
    plan: dict | None = None
    search_results: list = field(default_factory=list)   # [{query, provider, hits: [{title,url,snippet}]}]
    pages: list = field(default_factory=list)            # [{url, title, channel, content}]
    evidence: list = field(default_factory=list)         # [{id, claim, quote, url, title}]
    dropped_quotes: list = field(default_factory=list)   # 机械校验未通过（伪造/改写）的候选
    report: str | None = None
    report_truncated: bool = False
    verification: dict | None = None
    steps: list = field(default_factory=list)            # [StepRecord]
    cost_rmb: float = 0.0
    tokens: dict = field(default_factory=lambda: {"input_cache_hit": 0, "input_cache_miss": 0, "output": 0})
    error: str | None = None

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items()}
        d["steps"] = [s.to_dict() if isinstance(s, StepRecord) else s for s in self.steps]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "SpikeState":
        st = cls(qid=d["qid"], question=d["question"])
        for k in ("plan", "search_results", "pages", "evidence", "dropped_quotes",
                  "report", "report_truncated", "verification", "cost_rmb", "tokens", "error"):
            if k in d:
                setattr(st, k, d[k])
        st.steps = [StepRecord(**s) if isinstance(s, dict) else s for s in d.get("steps", [])]
        return st

    def done_steps(self) -> set[str]:
        return {s.name for s in self.steps if s.error is None and s.finished_at}

    def add_tokens(self, res: LLMResult) -> None:
        self.cost_rmb += res.cost_rmb
        for k, v in res.tokens.items():
            self.tokens[k] = self.tokens.get(k, 0) + v


class Workspace:
    """out/<qid>/ 落盘管理：checkpoint + steps 日志 + 最终产物。"""

    def __init__(self, qid: str):
        self.dir = OUT / qid
        self.dir.mkdir(parents=True, exist_ok=True)

    def checkpoint_path(self) -> Path:
        return self.dir / "checkpoint.json"

    def save_checkpoint(self, state: SpikeState) -> None:
        atomic_write_json(self.checkpoint_path(), state.to_dict())

    def load_checkpoint(self) -> SpikeState | None:
        p = self.checkpoint_path()
        if not p.exists():
            return None
        try:
            return SpikeState.from_dict(json.loads(p.read_text(encoding="utf-8")))
        except Exception as e:
            log(f"  ! checkpoint 损坏（{e}），忽略并从头跑")
            return None

    def save_step(self, state: SpikeState, record: StepRecord) -> None:
        idx = len(state.steps)
        atomic_write_json(self.dir / "steps" / f"{idx:02d}_{record.name}.json", record.to_dict())

    def finalize(self, state: SpikeState) -> None:
        """写 report.md / evidence.json / meta.json 三件套。"""
        (self.dir / "report.md").write_text(state.report or "", encoding="utf-8")
        atomic_write_json(self.dir / "evidence.json", state.evidence)
        if state.dropped_quotes:
            atomic_write_json(self.dir / "dropped_quotes.json", state.dropped_quotes)
        verification = state.verification or {}
        meta = {
            "qid": state.qid,
            "question": state.question,
            "pipeline": "plan → search → fetch → extract → write → verify",
            "finished": state.error is None,
            "error": state.error,
            "cost_rmb": round(state.cost_rmb, 6),
            "prices_rmb_per_1m": PRICES,
            "tokens": state.tokens,
            "steps": [s.to_dict() if isinstance(s, StepRecord) else s for s in state.steps],
            "evidence_count": len(state.evidence),
            "report_truncated": state.report_truncated,
            "dropped_quotes": len(state.dropped_quotes),
            "invalid_citations": len(verification.get("invalid_citations") or []),
            "citation_accuracy": verification.get("accuracy"),
            "verification_coverage": verification.get("verification_coverage"),
            "coverage": verification.get("coverage"),
        }
        atomic_write_json(self.dir / "meta.json", meta)


# ---------------- 六个 Step ----------------

PLAN_PROMPT = """你是深度研究 Agent 的规划器。针对研究问题，拆解出回答它必须覆盖的子问题（3-5 个），并为每个子问题设计 1-2 个英文或中文搜索词（共 4-6 个，搜索词要具体、可检索，不要问句）。

只输出 JSON，格式：
{{"sub_questions": ["子问题1", ...], "queries": ["搜索词1", ...]}}"""


async def step_plan(state: SpikeState, ctx: dict) -> None:
    llm: LLMProvider = ctx["llm"]
    data, res = await llm.chat_json([
        {"role": "system", "content": "你输出严格的 JSON。"},
        {"role": "user", "content": f"研究问题：{state.question}\n\n{PLAN_PROMPT}"},
    ])
    state.add_tokens(res)
    queries = (data.get("queries") or [])[:MAX_SEARCH_QUERIES]
    if not queries:
        raise RuntimeError(f"规划器未产出搜索词: {data}")
    state.plan = {"sub_questions": data.get("sub_questions") or [], "queries": queries}
    ctx["plan_queries"] = queries


SEARCH_INPUT_SUMMARY = "queries"


async def step_search(state: SpikeState, ctx: dict) -> None:
    client: httpx.AsyncClient = ctx["client"]
    queries = state.plan["queries"]
    sem = asyncio.Semaphore(SEARCH_CONCURRENCY)

    async def one(q: str) -> dict:
        async with sem:
            provider, hits, err = await search_with_fallback(client, q)
            log(f"  search[{provider}] {q!r} → {len(hits)} 条" + (f"（{err}）" if err else ""))
            return {"query": q, "provider": provider, "hits": [h.__dict__ for h in hits],
                    "error": err}

    state.search_results = await asyncio.gather(*[one(q) for q in queries])


async def step_fetch(state: SpikeState, ctx: dict) -> None:
    client: httpx.AsyncClient = ctx["client"]
    # 去重 URL，保留首个标题和 Tavily 正文（抓取失败时的兜底）
    seen: dict[str, tuple[str, str]] = {}
    for sr in state.search_results:
        for h in sr["hits"]:
            url = (h.get("url") or "").strip()
            if url.startswith(("http://", "https://")) and url not in seen:
                seen[url] = (h.get("title", ""), h.get("raw_content") or "")
    urls = list(seen.items())[:MAX_PAGES]
    if not urls:
        raise RuntimeError("搜索阶段未得到任何 URL，检查搜索 Key 或网络")

    sem = asyncio.Semaphore(FETCH_CONCURRENCY)
    jina_state: dict = {"jina_ok": True}

    async def one(item: tuple[str, tuple[str, str]]) -> dict:
        url, (title, raw_content) = item
        async with sem:
            content, channel = await fetch_page(client, url, jina_state)
            if not content and len(raw_content) >= 200:
                content, channel = raw_content[:MAX_PAGE_CHARS], "tavily_raw"
                log(f"  fetch[tavily_raw] {url[:70]} → {len(content)} 字符（抓取失败，用搜索正文兜底）")
            else:
                log(f"  fetch[{channel}] {url[:70]} → {len(content or '')} 字符")
            return {"url": url, "title": title, "channel": channel, "content": content}

    results = await asyncio.gather(*[one(item) for item in urls])
    state.pages = [r for r in results if r["content"]]
    if not state.pages:
        raise RuntimeError("所有页面抓取失败，检查网络或 Jina/trafilatura 可用性")


EXTRACT_PROMPT = """你是研究助理。从网页正文中提取与当前研究问题相关的、可独立成立的事实论断。

硬性要求：
1. "quote" 必须是从正文【逐字复制】的片段（可用 … 表示省略），严禁改写、概括、翻译；
2. "claim" 是一句陈述该片段支持的事实（中文）；
3. 与问题无关或无法逐字引用的页面，relevant=false；
4. 最多提取 6 条，宁缺毋滥。

只输出 JSON，格式：
{{"relevant": true, "claims": [{{"claim": "事实论断", "quote": "原文逐字片段"}}]}}"""


async def step_extract(state: SpikeState, ctx: dict) -> None:
    llm: LLMProvider = ctx["llm"]
    sem = asyncio.Semaphore(EXTRACT_CONCURRENCY)

    async def one(page: dict, idx: int) -> list[dict]:
        async with sem:
            try:
                data, res = await llm.chat_json([
                    {"role": "system", "content": "你输出严格的 JSON，quote 必须逐字复制原文。"},
                    {"role": "user", "content":
                        f"研究问题：{state.question}\n\n"
                        f"来源：{page['title']}\n{page['url']}\n\n正文：\n{page['content']}\n\n"
                        f"{EXTRACT_PROMPT}"},
                ], max_tokens=4096)
            except Exception as e:
                log(f"  extract 失败 page#{idx} {page['url'][:60]}: {e}")
                return []
            state.add_tokens(res)
            if not data.get("relevant"):
                return []
            out = []
            dropped_here = 0
            for c in data.get("claims", [])[:6]:
                if not (c.get("claim") and c.get("quote")):
                    continue
                if not quote_in_page(c["quote"], page["content"]):
                    dropped_here += 1
                    state.dropped_quotes.append({
                        "claim": c["claim"], "quote": c["quote"],
                        "url": page["url"], "reason": "quote 未在原文逐字命中（伪造或改写）",
                    })
                    continue
                out.append({"claim": c["claim"], "quote": c["quote"],
                            "url": page["url"], "title": page["title"]})
            if dropped_here:
                log(f"  ! extract 机械校验丢弃 {dropped_here} 条伪造/改写 quote：{page['url'][:60]}")
            return out

    results = await asyncio.gather(*[one(p, i) for i, p in enumerate(state.pages)])
    claims = [c for group in results for c in group]
    state.evidence = [{"id": f"e{i + 1}", **c} for i, c in enumerate(claims)]
    log(f"  extract 共 {len(state.pages)} 页 → {len(state.evidence)} 条证据"
        f"（机械校验丢弃 {len(state.dropped_quotes)} 条）")
    if not state.evidence:
        raise RuntimeError("未提取到任何证据，Spike 无法继续（可检查问题是否可检索、页面是否相关）")


WRITE_PROMPT = """你是深度研究分析师。基于给定的证据列表（每条有编号、论断、原文引用、来源）写一份中文研究报告。

硬性要求：
1. 每个事实性论断单独成句，句子结束后标注引用，如 [e3]；同一句最多 2 个引用标记；
2. 只在该句内容能被引用直接支撑时标注；不要把多个来源的要点合并成一句长句；
3. 只能引用给定证据，严禁编造证据之外的任何事实（时间、数字、产品名、观点）；
4. 证据不足以回答的子问题，如实写"证据不足"，不要用部分证据拼凑结论；
5. 结构：标题 → 摘要（≤5 句）→ 3-5 节正文 → 研究局限；不要输出证据列表（evidence.json 已单独保存）；
6. 全文控制在 2500-3500 中文字以内。"""


def _select_evidence(evidence: list[dict], max_total: int = 30,
                     max_per_source: int = 4) -> list[dict]:
    """控制写入阶段的证据规模，同时保证来源多样性。"""
    used: dict[str, int] = {}
    selected = []
    for e in evidence:
        url = e["url"]
        if used.get(url, 0) >= max_per_source:
            continue
        selected.append(e)
        used[url] = used.get(url, 0) + 1
        if len(selected) >= max_total:
            break
    return selected


async def step_write(state: SpikeState, ctx: dict) -> None:
    llm: LLMProvider = ctx["llm"]
    selected = _select_evidence(state.evidence)
    evidence_text = "\n\n".join(
        f"[{e['id']}] 来源：{e['title']}（{e['url']}）\n论断：{e['claim']}\n原文：{e['quote']}"
        for e in selected)
    sub_qs = "\n".join(f"- {q}" for q in state.plan["sub_questions"])
    messages = [
        {"role": "system", "content": "你是严谨的研究分析师，只依据给定证据写作。"},
        {"role": "user", "content":
            f"研究问题：{state.question}\n\n需覆盖的子问题：\n{sub_qs}\n\n"
            f"证据列表（共 {len(selected)} 条）：\n{evidence_text}\n\n{WRITE_PROMPT}"},
    ]
    res = await llm.chat(messages, temperature=0.4, max_tokens=8192)
    state.add_tokens(res)
    state.report = res.text
    tail = (res.text or "").rstrip()
    state.report_truncated = bool(tail) and tail[-1] not in "。！？.!?）)"
    if state.report_truncated:
        log(f"  ! write 输出可能被截断（末尾：{tail[-40:]!r}），考虑限长或分节写作")
    log(f"  write 使用证据 {len(selected)}/{len(state.evidence)} 条")


VERIFY_PROMPT = """你是严格的引用审查员（LLM judge）。逐条检查本段报告中每处 [e#] 引用标记是否真正支持它所在位置的论断。

硬性要求：
1. claim 摘录范围：只摘录紧邻该 [e#] 标记的那一句（以句号/换行分界），不要合并相邻句子；
2. 判断时同时参考证据的来源标题：来源本身的信息（如公司名、平台名）可以作为支撑，但必须来自给定的 title/url 字段；
3. 判定标准：
- supported：引用证据的 claim/quote/title 与该论断一致且足以支撑；
- partial：部分支撑（论断了多个要点，证据只覆盖其中一部分）；
- unsupported：证据与该论断无关、矛盾，或证据不足以得出该论断。

只输出 JSON，格式：
{{"checks": [{{"citation": "e3", "claim": "论断原文摘录", "verdict": "supported|partial|unsupported", "reason": "一句理由"}}]}}"""

VERIFY_COVERAGE_PROMPT = """按子问题评估这份报告的内容覆盖度：
- covered：有实质内容且有证据支撑；
- partial：有涉及但内容单薄；
- missing：未涉及或证据不足。

只输出 JSON，格式：
{{"coverage": {{"子问题原文": "covered|partial|missing"}}}}"""


def calc_accuracy(checks: list[dict]) -> dict:
    total = len(checks)
    if total == 0:
        return {"total_citations": 0, "strict": None, "lenient": None,
                "supported": 0, "partial": 0, "unsupported": 0}
    supported = sum(1 for c in checks if c.get("verdict") == "supported")
    partial = sum(1 for c in checks if c.get("verdict") == "partial")
    unsupported = total - supported - partial
    return {
        "total_citations": total,
        "strict": round(supported / total, 3),            # 只看完全支撑
        "lenient": round((supported + partial) / total, 3),  # partial 算半对
        "supported": supported, "partial": partial, "unsupported": unsupported,
    }


def _split_units(text: str, max_marks: int) -> list[str]:
    """把文本切成引用标记数不超过 max_marks 的单元（优先按行，行内超限按句）。"""
    units = []
    for line in text.split("\n"):
        if len(re.findall(r"\[e\d+\]", line)) <= max_marks:
            units.append(line)
            continue
        buf = ""
        for sent in re.split(r"(?<=[。！？!?])\s+", line):
            if buf and len(re.findall(r"\[e\d+\]", buf + sent)) > max_marks:
                units.append(buf)
                buf = sent
            else:
                buf = f"{buf} {sent}" if buf else sent
        if buf:
            units.append(buf)
    return units


def _chunks_by_marks(text: str, max_marks: int = 15,
                     max_chars: int = 3500) -> list[str]:
    """按引用标记数切块。

    verify 的输出 token 与判定条数成正比：按字符切会切出 65 处标记的批次，
    模型输出直接撞上限。按标记数切才能保证每批都判得完。
    """
    chunks, cur, marks, size = [], [], 0, 0
    for unit in _split_units(text, max_marks):
        n = len(re.findall(r"\[e\d+\]", unit))
        if cur and (marks + n > max_marks or size + len(unit) + 1 > max_chars):
            chunks.append("\n".join(cur))
            cur, marks, size = [], 0, 0
        cur.append(unit)
        marks += n
        size += len(unit) + 1
    if cur:
        chunks.append("\n".join(cur))
    return chunks


def _normalize_checks(data: Any) -> list[dict]:
    """宽容解析 judge 输出：兼容 {"checks":[...]}、单个 check、裸数组三种形态。"""
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict) and isinstance(data.get("checks"), list):
        items = data["checks"]
    elif isinstance(data, dict) and "citation" in data:
        items = [data]
    else:
        items = []
    out = []
    for c in items:
        if not isinstance(c, dict) or not c.get("citation"):
            continue
        if c.get("verdict") not in ("supported", "partial", "unsupported"):
            continue
        out.append({"citation": c["citation"], "claim": c.get("claim", ""),
                    "verdict": c["verdict"], "reason": c.get("reason", "")})
    return out


async def step_verify(state: SpikeState, ctx: dict) -> None:
    llm: LLMProvider = ctx["llm"]
    ev_by_id = {e["id"]: e for e in state.evidence}
    report = state.report or ""
    total_marks = len(re.findall(r"\[e\d+\]", report))
    checks: list[dict] = []

    async def verify_chunk(chunk: str, depth: int = 0, label: str = "") -> list[dict]:
        cited = sorted(set(re.findall(r"\[(e\d+)\]", chunk)), key=lambda x: int(x[1:]))
        # 只把本批实际引用的证据发给 judge，省 token 也减少干扰
        ev_text = "\n\n".join(
            f"[{i}] 来源：{ev_by_id[i]['title']}（{ev_by_id[i]['url']}）\n"
            f"论断：{ev_by_id[i]['claim']} | 原文：{ev_by_id[i]['quote'][:200]}"
            for i in cited if i in ev_by_id)
        part: list[dict] = []
        try:
            data, res = await llm.chat_json([
                {"role": "system", "content": "你是严格的引用审查员，只输出 JSON。"},
                {"role": "user", "content":
                    f"研究问题：{state.question}\n\n本批引用涉及的证据：\n{ev_text}\n\n"
                    f"报告片段：\n{chunk}\n\n{VERIFY_PROMPT}"},
            ], max_tokens=6144)
            state.add_tokens(res)
            part = _normalize_checks(data)
        except Exception as e:
            log(f"  ! verify 批次 {label} 失败: {type(e).__name__}: {e}")

        expected = len(re.findall(r"\[e\d+\]", chunk))
        # 判定数不足一半视为输出溢出/退化：拆半重试一次
        if len(part) < expected * 0.5 and depth == 0 and len(chunk) > 800:
            log(f"  ! 批次 {label} 仅 {len(part)}/{expected} 条判定，拆半重试")
            mid = len(chunk) // 2
            cut = chunk.rfind("\n", 0, mid)
            if cut < 200:
                cut = mid
            return (await verify_chunk(chunk[:cut], depth + 1, f"{label}a")
                    + await verify_chunk(chunk[cut:], depth + 1, f"{label}b"))
        log(f"  verify 批次 {label}: 引用 {len(cited)} 个 id / {expected} 处标记 → {len(part)} 条判定")
        return part

    chunks = _chunks_by_marks(report, max_marks=15, max_chars=3500)
    for bi, chunk in enumerate(chunks):
        checks.extend(await verify_chunk(chunk, label=str(bi + 1)))

    coverage = None
    try:
        cdata, cres = await llm.chat_json([
            {"role": "system", "content": "你只输出 JSON。"},
            {"role": "user", "content":
                f"研究问题：{state.question}\n\n"
                f"子问题：{json.dumps(state.plan['sub_questions'], ensure_ascii=False)}\n\n"
                f"报告：\n{report}\n\n{VERIFY_COVERAGE_PROMPT}"},
        ], max_tokens=1024)
        state.add_tokens(cres)
        coverage = cdata.get("coverage") if isinstance(cdata, dict) else None
    except Exception as e:
        log(f"  ! verify 覆盖度评估失败: {type(e).__name__}: {e}")

    invalid = sorted({c for c in re.findall(r"\[(e\d+)\]", report) if c not in ev_by_id},
                     key=lambda x: int(x[1:]))
    accuracy = calc_accuracy(checks)
    verified_ratio = round(len(checks) / total_marks, 3) if total_marks else None
    state.verification = {
        "checks": checks,
        "coverage": coverage,
        "invalid_citations": invalid,
        "total_marks": total_marks,
        "verified_marks": len(checks),
        "verification_coverage": verified_ratio,
        "accuracy": accuracy,
    }
    if checks:
        log(f"  verify 引用准确率: strict={accuracy['strict']} lenient={accuracy['lenient']} "
            f"({accuracy['supported']}支持/{accuracy['partial']}部分/"
            f"{accuracy['unsupported']}不支持)，无效编号 {len(invalid)} 个，"
            f"校验覆盖率 {verified_ratio:.0%}")
        if verified_ratio is not None and verified_ratio < 0.7:
            log("  ! 校验覆盖率低于 70%，Accuracy 代表性不足，建议检查 verify 批次日志")
    else:
        log(f"  ! verify 未得到任何有效判定，accuracy 不可用（无效编号 {len(invalid)} 个）")


# ---------------- 编排 ----------------

PIPELINE: list[tuple[str, Callable]] = [
    ("plan", step_plan),
    ("search", step_search),
    ("fetch", step_fetch),
    ("extract", step_extract),
    ("write", step_write),
    ("verify", step_verify),
]

STEP_INPUTS = {
    "plan": lambda s: {"question": s.question},
    "search": lambda s: {"queries": s.plan["queries"]},
    "fetch": lambda s: {"url_count": sum(len(r["hits"]) for r in s.search_results)},
    "extract": lambda s: {"pages": len(s.pages)},
    "write": lambda s: {"evidence_count": len(s.evidence)},
    "verify": lambda s: {"report_chars": len(s.report or "")},
}
STEP_OUTPUTS = {
    "plan": lambda s: s.plan,
    "search": lambda s: s.search_results,
    "fetch": lambda s: [{"url": p["url"], "channel": p["channel"],
                         "chars": len(p["content"])} for p in s.pages],
    "extract": lambda s: s.evidence,
    "write": lambda s: {"report_chars": len(s.report or ""), "report_preview": (s.report or "")[:500]},
    "verify": lambda s: s.verification,
}


async def run_pipeline(question: dict, resume: bool = True, fresh: bool = False) -> SpikeState:
    qid, text = question["qid"], question["question"]
    ws = Workspace(qid)
    state = None if fresh else ws.load_checkpoint()
    if state is not None and state.question != text:
        log(f"  ! checkpoint 问题不匹配，重跑")
        state = None
    if state is None:
        state = SpikeState(qid=qid, question=text)
        log(f"=== [{qid}] 从头开始: {text}")
    else:
        done = state.done_steps()
        log(f"=== [{qid}] 续跑（已完成 {sorted(done)}），从下一步继续: {text}")
        if state.error:
            state.error = None  # 上次失败的 step 本次重试

    async with httpx.AsyncClient(follow_redirects=True) as client:
        llm = LLMProvider(
            "deepseek",
            os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            os.getenv("DEEPSEEK_API_KEY", ""),
            os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
            client,
        )
        if not llm.api_key:
            raise RuntimeError("未配置 DEEPSEEK_API_KEY，请先 cp .env.example .env 并填 Key")
        ctx = {"llm": llm, "client": client}

        for i, (name, fn) in enumerate(PIPELINE):
            if name in state.done_steps():
                log(f"-- skip {name}（已完成）")
                continue
            record = StepRecord(
                name=name,
                started_at=datetime.now(timezone.utc).isoformat(),
                input_summary=STEP_INPUTS[name](state),
            )
            log(f"→ step {i + 1}/6 [{name}]")
            started = time.monotonic()
            try:
                await fn(state, ctx)
            except Exception as e:
                state.error = f"{name}: {type(e).__name__}: {e}"
                record.error = state.error
                record.finished_at = datetime.now(timezone.utc).isoformat()
                record.duration_s = time.monotonic() - started
                state.steps.append(record)
                ws.save_step(state, record)
                ws.save_checkpoint(state)  # 失败也落 checkpoint：重启后从失败步重试
                log(f"  ✗ [{name}] 失败: {state.error}")
                log(f"  checkpoint 已保存 → 修复问题后重跑同一条命令即可续跑（不重复已完成步骤）")
                ws.finalize(state)
                return state
            record.finished_at = datetime.now(timezone.utc).isoformat()
            record.duration_s = time.monotonic() - started
            record.output_summary = STEP_OUTPUTS[name](state)
            record.cost_rmb = round(state.cost_rmb, 6)
            record.tokens = dict(state.tokens)
            state.steps.append(record)
            ws.save_step(state, record)
            ws.save_checkpoint(state)  # 每步完成立即落盘 → kill 后最多重跑当前步
            log(f"  ✓ [{name}] {record.duration_s:.1f}s 累计 ¥{state.cost_rmb:.4f}")

    ws.finalize(state)
    return state


def print_summary(results: list[SpikeState]) -> None:
    print("\n" + "=" * 90)
    print(f"{'qid':<5}{'结果':<6}{'耗时(s)':<10}{'成本(¥)':<12}{'证据数':<8}"
          f"{'引用准确率(strict/lenient)':<30}输出目录")
    print("-" * 90)
    for s in results:
        total_s = sum(st.duration_s for st in s.steps)
        acc = (s.verification or {}).get("accuracy", {})
        acc_s = (f"{acc['strict']}/{acc['lenient']}"
                 if acc and acc.get("strict") is not None else "-")
        status = "✗ 失败" if s.error else "✓ 完成"
        print(f"{s.qid:<5}{status:<6}{total_s:<10.0f}{s.cost_rmb:<12.4f}"
              f"{len(s.evidence):<8}{acc_s:<30}{OUT / s.qid}")
    print("=" * 90)
    failed = [s.qid for s in results if s.error]
    if failed:
        print(f"失败题目: {failed} — 重跑 python spike.py --qid <qid> 可续跑\n")


def main() -> None:
    # Windows 控制台 UTF-8，避免中文输出报 GBK 编码错误
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="DeepScope Spike")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--qid", help="跑单题，如 q1")
    group.add_argument("--all", action="store_true", help="顺序跑 questions.json 全部题目")
    parser.add_argument("--fresh", action="store_true", help="忽略 checkpoint 从头重跑")
    parser.add_argument("--limit", type=int, default=None, help="配合 --all，只跑前 N 题")
    args = parser.parse_args()

    questions = json.loads(QUESTIONS_FILE.read_text(encoding="utf-8"))
    if args.qid:
        questions = [q for q in questions if q["qid"] == args.qid]
        if not questions:
            sys.exit(f"questions.json 中不存在 qid={args.qid}")
    if args.limit:
        questions = questions[: args.limit]

    async def run_all():
        results = []
        for q in questions:
            try:
                results.append(await run_pipeline(q, fresh=args.fresh))
            except Exception as e:
                log(f"=== [{q['qid']}] 顶层异常: {type(e).__name__}: {e}")
                traceback.print_exc()
                results.append(SpikeState(qid=q["qid"], question=q["question"],
                                          error=f"top: {type(e).__name__}: {e}"))
        print_summary(results)

    asyncio.run(run_all())


if __name__ == "__main__":
    main()
