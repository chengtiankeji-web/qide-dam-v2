"""AI provider abstraction.

Default backend: Alibaba 通义千问 (DashScope) — Sam already has account access
and the Vision + Text-Embedding APIs are reasonable cost & quality for CN.

Both providers (DashScope / OpenAI) ship the same `tag_image()` /
`describe_image()` / `embed_text()` / `embed_image()` signatures so we can
swap by setting `AI_PROVIDER`.

If neither key is set, the service runs in **stub mode**: returns deterministic
fake outputs so the pipeline is exercised end-to-end during development.
"""
from __future__ import annotations

import base64
import hashlib
from typing import Any

import httpx

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

EMBED_DIM = 768  # alembic 001 created `embedding vector(768)` — keep aligned

DASHSCOPE_TEXT_EMBED_URL = (
    "https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding"
)
DASHSCOPE_VL_URL = (
    "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
)
DASHSCOPE_TEXT_GEN_URL = (
    "https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation"
)

# Model identifiers · 由 Sam 2026-05-08 拍板 · 2026-05-13 深夜 Sam 加 use-case 路由
VISION_MODEL = "qwen3-vl-plus"      # 图片打标 / alt-text / visual-description
TEXT_GEN_MODEL = "qwen3.6-flash"    # 短文 / 快响应 / 大量并发的默认模型
EMBED_MODEL = "text-embedding-v3"   # 向量 768 维 · 与 alembic vector(768) 列对齐


# ─── v3 P1.3 LLM Use-case Routing (2026-05-13 深夜) ──────────────────
# Sam 拍板按调研报告 handover/code-quality-and-llm-routing-strategy-2026-05-13.md
# 来源数据：qwen-3.6 (2026-04 发布) · qwen-long (1M tokens · 长文档专用)
#
# 用法：调 `text_gen_for("long_doc_consolidate", prompt, ...)` 而非裸 `text_gen(...)` ·
# 自动选最合适模型 · 加 fallback chain。
USE_CASE_MODELS: dict[str, list[str]] = {
    # 长文档总结 · Memory/Handover/Plans/Sources consolidate
    "long_doc_consolidate":  ["qwen-long", "qwen-plus", "qwen3.6-flash"],
    # 客户资料 intake / 字段抽取
    "intake_extract":        ["qwen-plus", "qwen-long", "qwen3.6-flash"],
    # 复杂推理 / Skills workflow / 法律 / 财务
    "deep_reasoning":        ["qwen-max", "qwen-plus", "qwen3.6-flash"],
    # 询盘分级 · 短文 + 高并发 · 6 要素 A/B/C/D
    "lead_classify":         ["qwen3.6-flash", "qwen-plus"],
    # SEO 写文 · Kiln / CMH 引擎 · 1500-2500 字 · 需要推理
    "seo_writer":            ["qwen-plus", "qwen-long", "qwen3.6-flash"],
    # 翻译 / 文案改写 · 中短
    "translate_rewrite":     ["qwen3.6-flash", "qwen-plus"],
    # 通用 · 不确定时用这个
    "generic":               ["qwen3.6-flash", "qwen-plus"],
}

# Use-case 默认 max_tokens 上限（输出长度）
USE_CASE_MAX_TOKENS: dict[str, int] = {
    "long_doc_consolidate": 4000,
    "intake_extract":       2000,
    "deep_reasoning":       3000,
    "lead_classify":        500,    # 分级结果短
    "seo_writer":           4000,
    "translate_rewrite":    2000,
    "generic":              1500,
}


def _stub_embedding(seed: str) -> list[float]:
    """Deterministic fake 768-dim vector for dev / test mode."""
    h = hashlib.sha256(seed.encode()).digest()
    # Tile to 768 floats in [-1, 1]
    raw = (h * ((EMBED_DIM // len(h)) + 1))[:EMBED_DIM]
    return [(b / 127.5) - 1.0 for b in raw]


def has_provider() -> bool:
    return bool(settings.DASHSCOPE_API_KEY or settings.OPENAI_API_KEY)


# ----- text embedding -----

def embed_text(text: str) -> list[float]:
    if not text:
        return _stub_embedding("empty")
    if not has_provider():
        return _stub_embedding(text)

    if settings.DASHSCOPE_API_KEY:
        try:
            resp = httpx.post(
                DASHSCOPE_TEXT_EMBED_URL,
                headers={
                    "Authorization": f"Bearer {settings.DASHSCOPE_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": EMBED_MODEL,
                    "input": {"texts": [text[:2048]]},
                    "parameters": {"dimension": EMBED_DIM},
                },
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()
            vec = data["output"]["embeddings"][0]["embedding"]
            if len(vec) != EMBED_DIM:
                vec = (vec + [0.0] * EMBED_DIM)[:EMBED_DIM]
            return vec
        except Exception as e:  # noqa: BLE001
            logger.warning("ai.embed.text.dashscope_failed", error=str(e))
            return _stub_embedding(text)

    # OpenAI fallback
    try:
        resp = httpx.post(
            "https://api.openai.com/v1/embeddings",
            headers={
                "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={"model": "text-embedding-3-small", "input": text[:2048],
                  "dimensions": EMBED_DIM},
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]
    except Exception as e:  # noqa: BLE001
        logger.warning("ai.embed.text.openai_failed", error=str(e))
        return _stub_embedding(text)


def embed_image(image_bytes: bytes, *, hint_text: str = "") -> list[float]:
    """Embed an image. Strategy: caption it via VL model, then embed the caption.

    This gives semantic embeddings without needing a CLIP-style image model
    deployment. For cases where Sam later wants pure-visual matching (find
    duplicates / near-dup), Sprint 4 can add a CLIP backend.
    """
    if not has_provider():
        return _stub_embedding(hashlib.sha256(image_bytes[:4096]).hexdigest())
    caption = describe_image(image_bytes, prompt="一句话描述这张图，用于检索")
    text_for_embed = (hint_text + "\n" + caption).strip()
    return embed_text(text_for_embed)


# ----- vision -----

def describe_image(
    image_bytes: bytes, *, prompt: str = "请用一段中文描述这张图片的视觉内容"
) -> str:
    if not has_provider():
        return f"[stub] {prompt}"
    if settings.DASHSCOPE_API_KEY:
        try:
            b64 = base64.b64encode(image_bytes).decode()
            resp = httpx.post(
                DASHSCOPE_VL_URL,
                headers={
                    "Authorization": f"Bearer {settings.DASHSCOPE_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": VISION_MODEL,
                    "input": {
                        "messages": [
                            {
                                "role": "user",
                                "content": [
                                    {"image": f"data:image/jpeg;base64,{b64}"},
                                    {"text": prompt},
                                ],
                            }
                        ]
                    },
                },
                timeout=60.0,
            )
            resp.raise_for_status()
            data = resp.json()
            choices = data.get("output", {}).get("choices", [])
            if choices:
                content = choices[0].get("message", {}).get("content", [])
                # content is a list of { text: ... } dicts
                if isinstance(content, list):
                    return "".join(
                        c.get("text", "") for c in content if isinstance(c, dict)
                    )
                return str(content)
            return ""
        except Exception as e:  # noqa: BLE001
            logger.warning("ai.describe_image.dashscope_failed", error=str(e))
            return f"[error] {e}"

    # OpenAI Vision fallback
    try:
        b64 = base64.b64encode(image_bytes).decode()
        resp = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "gpt-4o-mini",
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                        },
                    ],
                }],
                "max_tokens": 300,
            },
            timeout=60.0,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:  # noqa: BLE001
        return f"[error] {e}"


# ----- text generation (qwen3.6-flash) -----

def text_gen_for(
    use_case: str,
    prompt: str,
    *,
    system: str | None = None,
    temperature: float = 0.5,
    max_tokens: int | None = None,
) -> str:
    """v3 P1.3 (2026-05-13 深夜) · use-case 路由 + 自动 fallback。

    use_case 必须在 USE_CASE_MODELS 中 · 否则 fall through 到 'generic'。

    模型选择按 USE_CASE_MODELS 表 · 第一个失败自动切第二个 · 都失败返 stub。
    每次切换 log 一行便于追溯实际用哪个模型。

    Sam 调研报告 handover/code-quality-and-llm-routing-strategy-2026-05-13.md
    """
    fallback_chain = USE_CASE_MODELS.get(use_case, USE_CASE_MODELS["generic"])
    effective_max_tokens = max_tokens or USE_CASE_MAX_TOKENS.get(use_case, 1500)

    last_error: Exception | None = None
    for model_name in fallback_chain:
        try:
            result = text_gen(
                prompt,
                system=system,
                max_tokens=effective_max_tokens,
                temperature=temperature,
                model=model_name,
            )
            # text_gen 失败返 "[error] ..." 字符串 · 不抛异常 · 这里判前缀决定是否 fallback
            if result.startswith("[error]"):
                logger.warning(
                    "ai.text_gen_for.model_failed",
                    use_case=use_case, model=model_name, error_prefix=result[:80],
                )
                last_error = RuntimeError(result)
                continue
            if result.startswith("[stub]"):
                # 没 provider key 时直接 stub · 不走 chain
                return result
            # 真成功 · log + 返
            logger.info("ai.text_gen_for.ok", use_case=use_case, model=model_name, output_len=len(result))
            return result
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "ai.text_gen_for.exception",
                use_case=use_case, model=model_name, error=str(e),
            )
            last_error = e
            continue

    # 全部模型都失败 · 返 stub 让 pipeline 不中断
    logger.error(
        "ai.text_gen_for.all_failed", use_case=use_case, chain=fallback_chain,
        last_error=str(last_error) if last_error else "?",
    )
    return f"[stub] text_gen_for({use_case}) · all models failed: {last_error}"


def text_gen(prompt: str, *, system: str | None = None, max_tokens: int = 1024,
             temperature: float = 0.5, model: str | None = None) -> str:
    """Pure text generation via DashScope qwen 系列。

    用途：文档总结 / 文案重写 / 视觉描述精简等。Sam 2026-05-08 拍板。

    v3 P1.3 (2026-05-13 晚): 加 model 参数 · 默认 qwen3.6-flash (短任务)
        consolidate 这种 100KB+ 输入场景显式传 model="qwen-plus" (128K context · 贵但够大)。

    返回纯文本（解析过 choice.message.content）。失败时返回 [error] 前缀。
    """
    if not has_provider():
        return f"[stub] text_gen({prompt[:40]}...)"

    effective_model = model or TEXT_GEN_MODEL

    if settings.DASHSCOPE_API_KEY:
        try:
            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": prompt})

            resp = httpx.post(
                DASHSCOPE_TEXT_GEN_URL,
                headers={
                    "Authorization": f"Bearer {settings.DASHSCOPE_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": effective_model,
                    "input": {"messages": messages},
                    "parameters": {
                        "result_format": "message",
                        "max_tokens": max_tokens,
                        "temperature": temperature,
                    },
                },
                # v3 P1.3 三修 (2026-05-13 深夜): qwen-long 100KB+ 输入可能 60-120s
                timeout=300.0,  # 5 min ceiling · 服务器 nginx 也要配长 timeout
            )
            resp.raise_for_status()
            data = resp.json()
            choices = data.get("output", {}).get("choices", [])
            if choices:
                return choices[0].get("message", {}).get("content", "")
            return ""
        except Exception as e:  # noqa: BLE001
            logger.warning("ai.text_gen.dashscope_failed", error=str(e))
            return f"[error] {e}"

    # OpenAI fallback (gpt-4o-mini)
    try:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        resp = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "gpt-4o-mini",
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
            timeout=60.0,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:  # noqa: BLE001
        return f"[error] {e}"


def tag_image(image_bytes: bytes) -> dict[str, Any]:
    """Returns { tags: list[str], summary: str, alt_text: str, visual_description: str }."""
    if not has_provider():
        return {
            "tags": ["stub"],
            "summary": "[stub] image tag",
            "alt_text": "[stub] alt text",
            "visual_description": "[stub] visual description",
        }
    prompt = (
        "请分析这张图片，返回 JSON 格式（不要 markdown 围栏），字段：\n"
        "- tags: 3-8 个中文标签数组\n"
        "- summary: 一句话总结（≤30 字）\n"
        "- alt_text: 一段无障碍 alt 文本（≤80 字，描述视觉而非诠释）\n"
        "- visual_description: 一段详细视觉描述（150-250 字）"
    )
    raw = describe_image(image_bytes, prompt=prompt)
    parsed: dict[str, Any] = {}
    try:
        import json
        # Strip markdown fences if present
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```", 2)[-1]
            cleaned = cleaned.split("```")[0]
        if cleaned.startswith("json\n"):
            cleaned = cleaned[5:]
        parsed = json.loads(cleaned)
    except Exception as e:  # noqa: BLE001
        logger.warning("ai.tag_image.parse_failed", error=str(e), raw=raw[:200])
        parsed = {"tags": [], "summary": raw[:60], "alt_text": raw[:80],
                  "visual_description": raw}
    parsed.setdefault("tags", [])
    parsed.setdefault("summary", "")
    parsed.setdefault("alt_text", "")
    parsed.setdefault("visual_description", "")
    return parsed


# ════════════════════════════════════════════════════════════
# JSON-mode completion · Smart Intake v4 用
# ════════════════════════════════════════════════════════════

def complete_json(
    prompt: str,
    *,
    system: str | None = None,
    max_tokens: int = 2048,
    temperature: float = 0.2,
) -> tuple[dict | list | None, dict]:
    """跟 text_gen 一样调 qwen3.6-flash · 但承诺解析 JSON

    返：(parsed_json_or_none, usage_info_dict)
      usage_info_dict 含 {input_tokens, output_tokens, cost_cny}
      ← 给 intake_service.bump_job_cost 累计用

    解析失败时 parsed=None · 调用方应该 fallback 到 rule-only
    """
    import json as _json

    usage = {"input_tokens": 0, "output_tokens": 0, "cost_cny": 0.0}

    if not has_provider():
        return None, usage   # caller falls back to rule-only

    if settings.DASHSCOPE_API_KEY:
        try:
            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": prompt})

            resp = httpx.post(
                DASHSCOPE_TEXT_GEN_URL,
                headers={
                    "Authorization": f"Bearer {settings.DASHSCOPE_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": TEXT_GEN_MODEL,
                    "input": {"messages": messages},
                    "parameters": {
                        "result_format": "message",
                        "max_tokens": max_tokens,
                        "temperature": temperature,
                        "response_format": {"type": "json_object"},
                    },
                },
                timeout=60.0,
            )
            resp.raise_for_status()
            data = resp.json()
            choices = data.get("output", {}).get("choices", [])
            raw = ""
            if choices:
                raw = choices[0].get("message", {}).get("content", "")

            # Token usage
            tok = data.get("usage", {})
            usage["input_tokens"] = int(tok.get("input_tokens", 0))
            usage["output_tokens"] = int(tok.get("output_tokens", 0))
            # qwen3.6-flash 价：input ¥0.0008/1K, output ¥0.002/1K
            usage["cost_cny"] = round(
                usage["input_tokens"] / 1000 * 0.0008
                + usage["output_tokens"] / 1000 * 0.002,
                6,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("ai.complete_json.dashscope_failed", error=str(e))
            return None, usage
    else:
        # OpenAI fallback
        try:
            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": prompt})
            resp = httpx.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "gpt-4o-mini",
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "response_format": {"type": "json_object"},
                },
                timeout=60.0,
            )
            resp.raise_for_status()
            data = resp.json()
            raw = data["choices"][0]["message"]["content"]
            tok = data.get("usage", {})
            usage["input_tokens"] = int(tok.get("prompt_tokens", 0))
            usage["output_tokens"] = int(tok.get("completion_tokens", 0))
            # gpt-4o-mini: input $0.15/1M, output $0.60/1M · 折 ¥ 1USD=7.2
            usage["cost_cny"] = round(
                usage["input_tokens"] / 1_000_000 * 0.15 * 7.2
                + usage["output_tokens"] / 1_000_000 * 0.60 * 7.2,
                6,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("ai.complete_json.openai_failed", error=str(e))
            return None, usage

    # 解析 JSON
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```", 2)[-1]
        cleaned = cleaned.split("```")[0]
    if cleaned.startswith("json\n"):
        cleaned = cleaned[5:]
    try:
        parsed = _json.loads(cleaned)
        return parsed, usage
    except _json.JSONDecodeError as e:
        logger.warning("ai.complete_json.parse_failed", error=str(e), raw=raw[:200])
        return None, usage
