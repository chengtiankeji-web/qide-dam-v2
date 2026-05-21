"""QideMatrix · 社媒爆款话题监测 Phase A · Reddit 监测 + AI 候选话题打分

Revision ID: 016_qm_topic_monitor
Revises: 015_qm_social_matrix
Create Date: 2026-05-15

═══════════════════════════════════════════════════════════════════════
背景：
═══════════════════════════════════════════════════════════════════════
SEO 工作流升级 · 加入"爆款话题嗅探"步骤：

  Phase A 流程（每天 06:00 CST · 06:02 接 SEO writer）：
    1. Reddit API 抓 7 个 B2B 制造业 subreddit 的 top 20 帖子 + top 10 评论
    2. LLM 用 lead_classify 路由打分（B2B 相关性 / 搜索意图 / 跟既有 backlog 不重叠 / 工厂匹配）
    3. 综合分 ≥ 阈值（28/40）的进 candidates 队列
    4. Top 3 推到 Sam 微信 · Sam 选 1 个 shortlist
    5. shortlist 进既有 SEO writer · 加 reddit 话题上下文 prompt

3 张新表：
  qm_topic_sources         · 监测源配置（subreddit / HN / Twitter / etc）
  qm_topic_signals         · 抓到的原始信号（post + top 评论）
  qm_topic_candidates      · LLM 评分后的候选话题 + 工作流状态

种子数据：7 个 B2B 工厂相关 subreddit
═══════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Union

from alembic import op

revision: str = "016_qm_topic_monitor"
down_revision: Union[str, None] = "015_qm_social_matrix"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ─── 1. qm_topic_sources · 监测源 ───────────────────────────────
    op.execute(
        """
        CREATE TABLE qm_topic_sources (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            workspace_id UUID REFERENCES qm_workspaces(id) ON DELETE CASCADE,

            source_type VARCHAR(20) NOT NULL,
            source_identifier VARCHAR(200) NOT NULL,
            display_name VARCHAR(200) NOT NULL,
            description TEXT,
            industry_tags TEXT[] DEFAULT '{}',

            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            fetch_top_n INT NOT NULL DEFAULT 20,
            fetch_comments_n INT NOT NULL DEFAULT 10,
            fetch_window_hours INT NOT NULL DEFAULT 24,

            last_fetched_at TIMESTAMPTZ,
            last_fetch_count INT,
            consecutive_failures INT NOT NULL DEFAULT 0,

            metadata JSONB DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

            CONSTRAINT uq_qm_topic_source UNIQUE (workspace_id, source_type, source_identifier),
            CONSTRAINT chk_qm_topic_source_type
                CHECK (source_type IN ('reddit', 'hackernews', 'twitter', 'quora', 'zhihu', 'weibo'))
        );
        CREATE INDEX idx_qm_topic_sources_enabled
            ON qm_topic_sources(workspace_id, enabled) WHERE enabled = TRUE;
        """
    )

    # ─── 2. qm_topic_signals · 抓到的原始信号 ────────────────────────
    op.execute(
        """
        CREATE TABLE qm_topic_signals (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            workspace_id UUID REFERENCES qm_workspaces(id) ON DELETE CASCADE,
            source_id UUID REFERENCES qm_topic_sources(id) ON DELETE CASCADE,

            external_id VARCHAR(200) NOT NULL,
            external_url TEXT,
            title TEXT,
            body TEXT,
            author_handle VARCHAR(100),
            score INT,
            num_comments INT,
            top_comments JSONB DEFAULT '[]'::jsonb,
            posted_at TIMESTAMPTZ,
            fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

            metadata JSONB DEFAULT '{}'::jsonb,

            CONSTRAINT uq_qm_signal_external UNIQUE (source_id, external_id)
        );
        CREATE INDEX idx_qm_signals_fetched
            ON qm_topic_signals(fetched_at DESC);
        CREATE INDEX idx_qm_signals_workspace_posted
            ON qm_topic_signals(workspace_id, posted_at DESC);
        CREATE INDEX idx_qm_signals_score
            ON qm_topic_signals(score DESC) WHERE score IS NOT NULL;
        """
    )

    # ─── 3. qm_topic_candidates · LLM 评分候选话题 + 工作流 ──────────
    op.execute(
        """
        CREATE TABLE qm_topic_candidates (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            workspace_id UUID REFERENCES qm_workspaces(id) ON DELETE CASCADE,
            signal_id UUID REFERENCES qm_topic_signals(id) ON DELETE CASCADE,

            -- AI 评分（0-10 各项）
            b2b_relevance INT,
            search_intent INT,
            coverage_novelty INT,
            factory_match INT,
            composite_score INT,

            -- AI 提炼
            distilled_topic VARCHAR(500),
            distilled_angle TEXT,
            suggested_title VARCHAR(500),
            suggested_keywords TEXT[] DEFAULT '{}',
            target_buyer_persona TEXT,

            -- 工作流状态
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            shortlisted_at TIMESTAMPTZ,
            shortlisted_by_user_id UUID REFERENCES users(id),
            dismissed_reason TEXT,
            written_post_id UUID REFERENCES qm_social_posts(id),
            wrote_at TIMESTAMPTZ,

            -- 元数据
            ai_model VARCHAR(50),
            ai_cost_cny_cents INT NOT NULL DEFAULT 0,
            ai_processing_time_ms INT,

            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

            CONSTRAINT chk_qm_candidate_status
                CHECK (status IN ('pending', 'shortlisted', 'written', 'dismissed')),
            CONSTRAINT chk_qm_candidate_scores
                CHECK (
                    (b2b_relevance IS NULL OR (b2b_relevance >= 0 AND b2b_relevance <= 10))
                    AND (search_intent IS NULL OR (search_intent >= 0 AND search_intent <= 10))
                    AND (coverage_novelty IS NULL OR (coverage_novelty >= 0 AND coverage_novelty <= 10))
                    AND (factory_match IS NULL OR (factory_match >= 0 AND factory_match <= 10))
                )
        );
        CREATE INDEX idx_qm_candidates_pending_top
            ON qm_topic_candidates(workspace_id, composite_score DESC)
            WHERE status = 'pending';
        CREATE INDEX idx_qm_candidates_status
            ON qm_topic_candidates(status, created_at DESC);
        CREATE UNIQUE INDEX uq_qm_candidate_signal
            ON qm_topic_candidates(signal_id);
        """
    )

    # ─── 4. 种子 · 7 个 B2B 工厂相关 subreddit（绑 internal workspace） ──
    # 用 internal workspace（Sam 自营空间）作为默认 monitor 范围
    op.execute(
        """
        WITH internal_ws AS (
            SELECT w.id FROM qm_workspaces w
            JOIN tenants t ON t.id = w.tenant_id
            WHERE t.slug = 'qide' AND w.slug = 'qide-internal'
            LIMIT 1
        )
        INSERT INTO qm_topic_sources (
            workspace_id, source_type, source_identifier, display_name,
            description, industry_tags, fetch_top_n, fetch_comments_n, enabled
        )
        SELECT
            iw.id, 'reddit', src.identifier, src.display_name,
            src.description, src.tags, 20, 10, TRUE
        FROM internal_ws iw
        CROSS JOIN (VALUES
            ('manufacturing', 'r/manufacturing', '工业制造讨论 · 工厂主 + 工程师', ARRAY['manufacturing', 'b2b', 'industrial']),
            ('Entrepreneur', 'r/Entrepreneur', '创业者社群 · 经常聊找工厂 / 供应链', ARRAY['founder', 'sourcing', 'startup']),
            ('AmazonFBA', 'r/AmazonFBA', '亚马逊卖家 · 自有品牌找 OEM/ODM 工厂', ARRAY['amazon', 'private_label', 'sourcing']),
            ('PrivateLabel', 'r/PrivateLabel', '自有品牌卖家 · 直接 OEM 需求', ARRAY['private_label', 'sourcing']),
            ('AlibabaSourcing', 'r/AlibabaSourcing', '阿里国际站采购经验分享 · 黄金 sub', ARRAY['alibaba', 'sourcing', 'b2b']),
            ('SmallBusiness', 'r/smallbusiness', '小企业主 · 包括外贸/批发', ARRAY['smb', 'business']),
            ('sourcing', 'r/sourcing', '专业采购话题 · 流量小但精准', ARRAY['sourcing', 'procurement'])
        ) AS src(identifier, display_name, description, tags)
        ON CONFLICT DO NOTHING;
        """
    )

    # ─── 5. audit 留痕 ────────────────────────────────────────────────
    op.execute(
        """
        INSERT INTO audit_events (
            tenant_id, project_id, actor_user_id, actor_kind,
            action, target_kind, target_id, status, purpose,
            ip, user_agent, metadata
        )
        SELECT
            t.id, NULL, NULL, 'system',
            'audit.migration.applied', 'schema', NULL,
            'success',
            '016_qm_topic_monitor · 3 表 + 7 subreddit 种子',
            NULL, NULL,
            jsonb_build_object(
                'migration', '016_qm_topic_monitor',
                'tables', 3,
                'seed_subreddits', 7,
                'actor_label', 'alembic_016_topic_monitor'
            )
        FROM tenants t WHERE t.slug = 'qide' LIMIT 1;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS qm_topic_candidates CASCADE;")
    op.execute("DROP TABLE IF EXISTS qm_topic_signals CASCADE;")
    op.execute("DROP TABLE IF EXISTS qm_topic_sources CASCADE;")
