"""
最小 API 验证：DeepSeek / SiliconFlow / Tavily / 博查 各跑一行，确认 Key 可用。

特别注意 Tavily 的网络检查：当前网络不通 api.tavily.com 时（国内常见），
spike.py 会自动降级博查，因此验证结果只影响"主力/备用"顺序，不影响能否跑 Spike。

用法：先 cp .env.example .env 并填 Key，然后 python check_apis.py
"""

import asyncio
import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()


async def check_deepseek(client: httpx.AsyncClient) -> tuple[str, str]:
    key = os.getenv("DEEPSEEK_API_KEY", "")
    if not key:
        return "跳过", "未配置 DEEPSEEK_API_KEY"
    base = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
    try:
        resp = await client.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": model,
                  "messages": [{"role": "user", "content": "只回复两个字：收到"}],
                  "max_tokens": 10},
            timeout=httpx.Timeout(30.0),
        )
        if resp.status_code != 200:
            return "✗ 失败", f"HTTP {resp.status_code}: {resp.text[:200]}"
        data = resp.json()
        usage = data.get("usage", {})
        text = data["choices"][0]["message"]["content"]
        return "✓ 通过", (f"模型={model} 回复={text!r} "
                          f"hit={usage.get('prompt_cache_hit_tokens', '?')} "
                          f"miss={usage.get('prompt_cache_miss_tokens', usage.get('prompt_tokens', '?'))}")
    except httpx.TimeoutException:
        return "✗ 失败", "连接超时（30s）"
    except httpx.TransportError as e:
        return "✗ 失败", f"网络错误: {type(e).__name__}"


async def check_siliconflow(client: httpx.AsyncClient) -> tuple[str, str]:
    key = os.getenv("SILICONFLOW_API_KEY", "")
    if not key:
        return "跳过", "未配置 SILICONFLOW_API_KEY（Spike 阶段仅 W2 起需要）"
    base = os.getenv("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1")
    model = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3")
    try:
        resp = await client.post(
            f"{base}/embeddings",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": model, "input": "DeepScope"},
            timeout=httpx.Timeout(30.0),
        )
        if resp.status_code != 200:
            return "✗ 失败", f"HTTP {resp.status_code}: {resp.text[:200]}"
        dim = len(resp.json()["data"][0]["embedding"])
        return "✓ 通过", f"模型={model} 向量维度={dim}"
    except httpx.TimeoutException:
        return "✗ 失败", "连接超时（30s）"
    except httpx.TransportError as e:
        return "✗ 失败", f"网络错误: {type(e).__name__}"


async def check_tavily(client: httpx.AsyncClient) -> tuple[str, str]:
    """Key 无效（4xx）与网络不通（连接错误/超时）分开报告，决定是否切博查。"""
    key = os.getenv("TAVILY_API_KEY", "")
    if not key:
        return "跳过", "未配置 TAVILY_API_KEY"
    try:
        resp = await client.post(
            "https://api.tavily.com/search",
            json={"api_key": key, "query": "LangGraph", "max_results": 3},
            timeout=httpx.Timeout(20.0),
        )
        if resp.status_code in (401, 403):
            return "✗ Key 无效", f"HTTP {resp.status_code}（检查 Key 或套餐额度）"
        if resp.status_code != 200:
            return "✗ 失败", f"HTTP {resp.status_code}: {resp.text[:200]}"
        n = len(resp.json().get("results", []))
        return "✓ 通过", f"返回 {n} 条结果"
    except httpx.TimeoutException:
        return "⚠ 网络不通", "超时（20s）——当前网络无法访问 api.tavily.com，Spike 将自动降级博查"
    except httpx.TransportError as e:
        return "⚠ 网络不通", f"{type(e).__name__}——当前网络无法访问 api.tavily.com，Spike 将自动降级博查"


async def check_bocha(client: httpx.AsyncClient) -> tuple[str, str]:
    key = os.getenv("BOCHA_API_KEY", "")
    if not key:
        return "跳过", "未配置 BOCHA_API_KEY"
    try:
        resp = await client.post(
            "https://api.bochaai.com/v1/web-search",
            headers={"Authorization": f"Bearer {key}"},
            json={"query": "LangGraph", "freshness": "noLimit", "summary": False, "count": 3},
            timeout=httpx.Timeout(20.0),
        )
        if resp.status_code in (401, 403):
            return "✗ Key 无效", f"HTTP {resp.status_code}（检查 Key 或套餐额度）"
        if resp.status_code != 200:
            return "✗ 失败", f"HTTP {resp.status_code}: {resp.text[:200]}"
        data = resp.json()
        pages = (data.get("data", {}).get("webPages") or {}).get("value", []) or []
        return "✓ 通过", f"返回 {len(pages)} 条结果"
    except httpx.TimeoutException:
        return "⚠ 网络不通", "超时（20s）——博查也不通的话，Spike 的搜索层将全部失败"
    except httpx.TransportError as e:
        return "⚠ 网络不通", f"{type(e).__name__}"


async def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if not os.path.exists(".env"):
        print("尚未创建 .env —— 请先执行：cp .env.example .env，填入各 API Key 后重跑本脚本")
        sys.exit(1)

    print("=" * 70)
    print("DeepScope API 验证（各跑一行真实调用）")
    print("=" * 70)
    async with httpx.AsyncClient(follow_redirects=True) as client:
        for name, fn in [("DeepSeek（LLM 主力）", check_deepseek),
                         ("SiliconFlow（Embedding）", check_siliconflow),
                         ("Tavily（搜索主力）", check_tavily),
                         ("博查（搜索备用）", check_bocha)]:
            status, detail = await fn(client)
            print(f"  {name:<24} {status:<12} {detail}")
    print("=" * 70)
    print("下一步：python spike.py --qid q1   （跑第一题验证全链路）")


if __name__ == "__main__":
    asyncio.run(main())
