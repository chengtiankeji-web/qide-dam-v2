#!/usr/bin/env python3
"""QideMatrix Phase A · 话题监测调度器 standalone runner

═══════════════════════════════════════════════════════════════════════
两种调用方式：
═══════════════════════════════════════════════════════════════════════

A. 本地 / Cowork scheduled task 触发（不需要 Celery worker 在线）
   $ cd /opt/qide-dam
   $ python -m scripts.topic_monitor_run
   $ python -m scripts.topic_monitor_run --workspace-id <uuid>
   $ python -m scripts.topic_monitor_run --skip-fetch  # 只跑评分
   $ python -m scripts.topic_monitor_run --skip-score  # 只抓数据

B. 真生产 · Celery beat 已经在 06:00 / 06:30 自动跑
   不需要手动调

═══════════════════════════════════════════════════════════════════════
输出：
═══════════════════════════════════════════════════════════════════════
打印 JSON 摘要到 stdout · 可被外层 scheduled task / shell 抓取再决定推送
  · fetch.sources_processed · 抓了几个 source
  · fetch.new_signals       · 新增 signal 数
  · score.scored            · LLM 评分成功数
  · score.above_threshold   · 综合分 >= 28 进 pending 候选数
  · score.cost_cny_cents    · 本次 LLM 总花销（分）
  · top_candidates          · top 3 候选 [{id, score, title, persona}]

返回码：
  0 = success
  1 = exception
  2 = no sources to monitor / 0 new signals · 提醒去 admin SPA 启用监测源

═══════════════════════════════════════════════════════════════════════
适用场景：
═══════════════════════════════════════════════════════════════════════
1. Cowork scheduled task · 每天 06:02 触发 · Run now 也可手动
2. 排查问题：手动跑一次看返 JSON
3. 老板 Sam 早上想看候选话题：脚本跑完 → top_candidates 推微信
═══════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from typing import Any


async def _run(
    workspace_id: str | None,
    skip_fetch: bool,
    skip_score: bool,
    score_limit: int,
    top_n: int,
    min_score: int,
) -> dict:
    from app.db.session import get_session_factory
    from app.services.qidematrix import topic_monitor_service as tms

    ws_uuid = uuid.UUID(workspace_id) if workspace_id else None
    session_factory = get_session_factory()

    fetch_result: dict[str, Any] = {"skipped": True}
    score_result: dict[str, Any] = {"skipped": True}

    # ─── 1. fetch ──────────────────────────────────────────────────
    if not skip_fetch:
        async with session_factory() as db:
            fetch_result = await tms.fetch_and_store_all_enabled(
                db, workspace_id=ws_uuid
            )

    # ─── 2. score ──────────────────────────────────────────────────
    if not skip_score:
        async with session_factory() as db:
            score_result = await tms.score_unscored_signals(
                db, workspace_id=ws_uuid, limit=score_limit
            )

    # ─── 3. 列 top N 候选 ──────────────────────────────────────────
    async with session_factory() as db:
        top = await tms.list_top_pending_candidates(
            db, workspace_id=ws_uuid, top_n=top_n, min_score=min_score
        )
        top_summary = [
            {
                "id": str(c.id),
                "score": c.composite_score,
                "title": c.suggested_title or c.distilled_topic,
                "keywords": list(c.suggested_keywords or []),
                "persona": c.target_buyer_persona,
            }
            for c in top
        ]

    return {
        "fetch": fetch_result,
        "score": score_result,
        "top_candidates": top_summary,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="QideMatrix · Reddit 话题监测 一键脚本（fetch + score + top）"
    )
    parser.add_argument(
        "--workspace-id",
        default=None,
        help="限定 workspace UUID（默认全 workspace · 当前生产仅 qide-internal）",
    )
    parser.add_argument(
        "--skip-fetch", action="store_true", help="只跑评分 · 不抓 Reddit"
    )
    parser.add_argument(
        "--skip-score", action="store_true", help="只抓数据 · 不跑 LLM"
    )
    parser.add_argument(
        "--score-limit",
        type=int,
        default=200,
        help="单次 LLM 评分最多处理多少 signal（防止 cost 爆炸）",
    )
    parser.add_argument(
        "--top-n", type=int, default=3, help="返 top N 候选（默认 3）"
    )
    parser.add_argument(
        "--min-score", type=int, default=28, help="候选最低综合分（满分 40）"
    )
    args = parser.parse_args()

    try:
        result = asyncio.run(
            _run(
                workspace_id=args.workspace_id,
                skip_fetch=args.skip_fetch,
                skip_score=args.skip_score,
                score_limit=args.score_limit,
                top_n=args.top_n,
                min_score=args.min_score,
            )
        )
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))

    # 退出码：让 scheduled task 能感知"是不是该提醒老板加监测源"
    fetched = result.get("fetch", {}).get("new_signals", 0) or 0
    sources = result.get("fetch", {}).get("sources_processed", 0) or 0
    if sources == 0:
        return 2  # 0 个监测源 enabled
    if fetched == 0 and not result.get("top_candidates"):
        return 2  # 抓到 0 新 + 0 候选 · 推一下让 Sam 看下源是否还能用
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
