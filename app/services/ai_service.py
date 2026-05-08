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

# Model identifiers · 由 Sam 2026-05-08 拍板
VISION_MODEL = "qwen3-vl-plus"      # 图片打标 / alt-text / visual-description
TEXT_GEN_MODEL = "qwen3.6-flash"    # 文档总结 / 文案重写 / 未来文本任务
EMBED_MODEL = "text-embedding-v3"   # 向量 768 维 · 与 alembic vector(768) 列对齐


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

def text_gen(prompt: str, *, system: str | None = None, max_tokens: int = 1024,
             temperature: float = 0.5) -> str:
    """Pure text generation via qwen3.6-flash.

    用途：文档总结 / 文案重写 / 视觉描述精简等。当前 pipeline 没串入，
    给未来 tasks_document / tasks_text_summary 备用。Sam 2026-05-08 拍板。

    返回纯文本（解析过 choice.message.content）。失败时返回 [error] 前缀。
    """
    if not has_provider():
        return f"[stub] text_gen({prompt[:40]}...)"

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
                    },
                },
                timeout=60.0,
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
