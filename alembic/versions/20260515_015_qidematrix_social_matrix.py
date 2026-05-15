"""QideMatrix 社媒矩阵 · 6 张表 · 账号管家核心 schema

Revision ID: 015_qm_social_matrix
Revises: 014_qidematrix_core
Create Date: 2026-05-15

═══════════════════════════════════════════════════════════════════════
定位升级：祁德社媒矩阵 (QideMatrix) · 制造业外贸工厂获客工具
═══════════════════════════════════════════════════════════════════════
昨晚战略报告 + 今晨命名 + 痛点细化后增加的核心模块：

  矩阵账号管家 (Matrix Account Custodian)
  ├── 平台账号档案 (qm_social_accounts)
  ├── 浏览器环境绑定 (qm_browser_profiles · AdsPower / Multilogin 等)
  ├── 代理 IP 池 (qm_proxy_pool · Bright Data / IPRoyal 等)
  ├── 跨平台内容池 (qm_social_posts · 1 原稿 → N 平台改写)
  ├── 发布调度 (qm_post_schedules · 随机偏移 + 时区一致防 bot)
  └── 健康度事件流 (qm_account_health_events · 风控警报)

═══════════════════════════════════════════════════════════════════════
关键设计原则：
═══════════════════════════════════════════════════════════════════════

1. **凭证全部 Vault 加密** · credentials_vault_id 引 QideDAM Vault · 不存明文
2. **浏览器指纹隔离不自研** · 通过 external_profile_id 接 AdsPower API
3. **每账号必须独立 (浏览器环境 + 代理 IP + 地理时区)** 三要素绑定
4. **健康度事件不可改** · trigger 锁死 UPDATE / DELETE · 风控追溯凭证
5. **发布时间随机偏移** · scheduled_at vs actual_published_at 差值最大 ±30 分钟
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Union

from alembic import op

revision: str = "015_qm_social_matrix"
down_revision: Union[str, None] = "014_qidematrix_core"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ─── 1. qm_social_accounts ────────────────────────────────────────
    op.execute(
        """
        CREATE TABLE qm_social_accounts (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            workspace_id UUID NOT NULL REFERENCES qm_workspaces(id) ON DELETE CASCADE,

            -- 平台 + 用途
            platform VARCHAR(30) NOT NULL,
            purpose VARCHAR(30) NOT NULL DEFAULT 'main',
            account_handle VARCHAR(200) NOT NULL,
            display_name VARCHAR(200),
            persona JSONB DEFAULT '{}'::jsonb,

            -- 隔离三要素（浏览器 + 代理 + 地理）
            browser_profile_id UUID,
            proxy_pool_id UUID,
            geo_country VARCHAR(2),
            geo_timezone VARCHAR(50),

            -- 凭证（Vault 加密 · 引 vault_assets.id）
            credentials_vault_id UUID,

            -- 状态 + 健康度
            status VARCHAR(20) NOT NULL DEFAULT 'active',
            health_score INT NOT NULL DEFAULT 100,
            last_login_at TIMESTAMPTZ,
            last_warning_at TIMESTAMPTZ,
            last_post_at TIMESTAMPTZ,

            -- 平台风控合规配额
            daily_post_limit INT,
            daily_follow_limit INT,
            daily_like_limit INT,

            metadata JSONB DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            deleted_at TIMESTAMPTZ,

            CONSTRAINT uq_qm_social_account_handle
                UNIQUE (workspace_id, platform, account_handle),
            CONSTRAINT chk_qm_social_platform
                CHECK (platform IN (
                    'linkedin_company', 'linkedin_personal',
                    'tiktok_business', 'tiktok_creator',
                    'instagram_business', 'instagram_creator',
                    'facebook_page', 'facebook_personal',
                    'x_twitter', 'youtube_channel',
                    'pinterest', 'reddit'
                )),
            CONSTRAINT chk_qm_social_purpose
                CHECK (purpose IN ('main', 'sales_rep', 'brand_amplifier', 'experiment', 'archive')),
            CONSTRAINT chk_qm_social_status
                CHECK (status IN ('active', 'suspended', 'limited', 'banned', 'archived', 'pending_setup')),
            CONSTRAINT chk_qm_social_health
                CHECK (health_score >= 0 AND health_score <= 100)
        );
        CREATE INDEX idx_qm_social_workspace_alive
            ON qm_social_accounts(workspace_id) WHERE deleted_at IS NULL;
        CREATE INDEX idx_qm_social_platform_status
            ON qm_social_accounts(platform, status) WHERE deleted_at IS NULL;
        CREATE INDEX idx_qm_social_health_low
            ON qm_social_accounts(health_score) WHERE health_score < 70 AND deleted_at IS NULL;
        """
    )

    # ─── 2. qm_browser_profiles ───────────────────────────────────────
    op.execute(
        """
        CREATE TABLE qm_browser_profiles (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            workspace_id UUID NOT NULL REFERENCES qm_workspaces(id) ON DELETE CASCADE,

            provider VARCHAR(20) NOT NULL DEFAULT 'adspower',
            external_profile_id VARCHAR(100) NOT NULL,
            profile_name VARCHAR(200) NOT NULL,

            -- Fingerprint 摘要（不存敏感细节）
            fingerprint_summary JSONB DEFAULT '{}'::jsonb,

            -- 状态机
            status VARCHAR(20) NOT NULL DEFAULT 'idle',
            last_opened_at TIMESTAMPTZ,
            last_closed_at TIMESTAMPTZ,
            open_count INT NOT NULL DEFAULT 0,

            metadata JSONB DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

            CONSTRAINT uq_qm_browser_external
                UNIQUE (workspace_id, provider, external_profile_id),
            CONSTRAINT chk_qm_browser_provider
                CHECK (provider IN ('adspower', 'multilogin', 'dolphin_anty', 'kameleo', 'bit_browser', 'vmlogin')),
            CONSTRAINT chk_qm_browser_status
                CHECK (status IN ('idle', 'active', 'open', 'closed', 'error', 'expired'))
        );
        CREATE INDEX idx_qm_browser_workspace
            ON qm_browser_profiles(workspace_id);
        CREATE INDEX idx_qm_browser_status
            ON qm_browser_profiles(status) WHERE status IN ('open', 'error');
        """
    )

    # ─── 3. qm_proxy_pool ─────────────────────────────────────────────
    op.execute(
        """
        CREATE TABLE qm_proxy_pool (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            workspace_id UUID NOT NULL REFERENCES qm_workspaces(id) ON DELETE CASCADE,

            provider VARCHAR(30) NOT NULL,
            proxy_type VARCHAR(20) NOT NULL,

            -- 地理（用于跟账号地理一致性匹配）
            country VARCHAR(2) NOT NULL,
            region VARCHAR(50),
            city VARCHAR(100),

            -- 连接信息
            host VARCHAR(200),
            port INT,
            -- 用户名密码用 Vault 加密 · 这里只存引用
            credentials_vault_id UUID,

            -- 流量配额
            monthly_quota_gb INT,
            used_gb_this_month REAL NOT NULL DEFAULT 0,

            -- 状态
            status VARCHAR(20) NOT NULL DEFAULT 'available',
            last_health_check_at TIMESTAMPTZ,
            consecutive_failures INT NOT NULL DEFAULT 0,
            avg_latency_ms INT,

            metadata JSONB DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

            CONSTRAINT chk_qm_proxy_provider
                CHECK (provider IN ('bright_data', 'smartproxy', 'iproyal', 'lunaproxy', 'oxylabs', '911_s5', 'soax', 'manual')),
            CONSTRAINT chk_qm_proxy_type
                CHECK (proxy_type IN ('residential', 'mobile', 'datacenter', 'isp')),
            CONSTRAINT chk_qm_proxy_status
                CHECK (status IN ('available', 'in_use', 'exhausted', 'failed', 'archived'))
        );
        CREATE INDEX idx_qm_proxy_workspace_status
            ON qm_proxy_pool(workspace_id, status);
        CREATE INDEX idx_qm_proxy_country_type_available
            ON qm_proxy_pool(country, proxy_type) WHERE status = 'available';
        """
    )

    # ─── 4. qm_social_posts ───────────────────────────────────────────
    op.execute(
        """
        CREATE TABLE qm_social_posts (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            workspace_id UUID NOT NULL REFERENCES qm_workspaces(id) ON DELETE CASCADE,

            -- 原稿
            original_title VARCHAR(500),
            original_body TEXT,
            original_media_asset_ids UUID[],

            -- 平台改写版本（一 JSON 装所有平台）
            -- 例：{"linkedin_company": {"body": "...", "hashtags": [...]}, "tiktok": {...}}
            platform_variants JSONB DEFAULT '{}'::jsonb,

            -- 状态机
            status VARCHAR(20) NOT NULL DEFAULT 'draft',
            approval_required BOOLEAN NOT NULL DEFAULT FALSE,
            approved_at TIMESTAMPTZ,
            approved_by_user_id UUID REFERENCES users(id),

            -- 元数据
            content_type VARCHAR(30),
            target_industry VARCHAR(50),
            ai_generated BOOLEAN NOT NULL DEFAULT FALSE,
            ai_use_case VARCHAR(50),

            created_by_user_id UUID REFERENCES users(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            deleted_at TIMESTAMPTZ,

            CONSTRAINT chk_qm_post_status
                CHECK (status IN ('draft', 'pending_approval', 'approved', 'publishing', 'published', 'archived'))
        );
        CREATE INDEX idx_qm_posts_workspace_status
            ON qm_social_posts(workspace_id, status) WHERE deleted_at IS NULL;
        CREATE INDEX idx_qm_posts_pending_approval
            ON qm_social_posts(workspace_id) WHERE status = 'pending_approval' AND deleted_at IS NULL;
        """
    )

    # ─── 5. qm_post_schedules ─────────────────────────────────────────
    op.execute(
        """
        CREATE TABLE qm_post_schedules (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            workspace_id UUID NOT NULL REFERENCES qm_workspaces(id) ON DELETE CASCADE,
            post_id UUID NOT NULL REFERENCES qm_social_posts(id) ON DELETE CASCADE,
            social_account_id UUID NOT NULL REFERENCES qm_social_accounts(id) ON DELETE CASCADE,

            -- 时间双轨：计划 vs 实际
            scheduled_at TIMESTAMPTZ NOT NULL,
            actual_published_at TIMESTAMPTZ,
            jitter_seconds INT DEFAULT 0,

            -- 状态
            status VARCHAR(20) NOT NULL DEFAULT 'pending',

            -- 平台返回
            platform_post_id VARCHAR(200),
            platform_post_url TEXT,

            -- 错误处理
            error_message TEXT,
            retry_count INT NOT NULL DEFAULT 0,
            max_retries INT NOT NULL DEFAULT 3,
            next_retry_at TIMESTAMPTZ,

            metadata JSONB DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

            CONSTRAINT chk_qm_schedule_status
                CHECK (status IN ('pending', 'publishing', 'published', 'failed', 'cancelled', 'retry_pending'))
        );
        CREATE INDEX idx_qm_schedule_due
            ON qm_post_schedules(scheduled_at) WHERE status IN ('pending', 'retry_pending');
        CREATE INDEX idx_qm_schedule_post
            ON qm_post_schedules(post_id);
        CREATE INDEX idx_qm_schedule_account
            ON qm_post_schedules(social_account_id, scheduled_at DESC);
        """
    )

    # ─── 6. qm_account_health_events · 不可篡改 ────────────────────────
    op.execute(
        """
        CREATE TABLE qm_account_health_events (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            workspace_id UUID NOT NULL REFERENCES qm_workspaces(id),
            social_account_id UUID NOT NULL REFERENCES qm_social_accounts(id),

            event_type VARCHAR(40) NOT NULL,
            severity VARCHAR(20) NOT NULL DEFAULT 'info',
            description TEXT,

            -- 上下文（事件发生时哪个浏览器 + 代理 IP）
            proxy_pool_id UUID,
            browser_profile_id UUID,
            triggered_by VARCHAR(40),

            -- 健康分变化（用于追溯）
            health_score_before INT,
            health_score_after INT,

            payload JSONB DEFAULT '{}'::jsonb,
            occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

            CONSTRAINT chk_qm_health_severity
                CHECK (severity IN ('info', 'warning', 'critical', 'fatal'))
        );
        CREATE INDEX idx_qm_health_account_time
            ON qm_account_health_events(social_account_id, occurred_at DESC);
        CREATE INDEX idx_qm_health_severity
            ON qm_account_health_events(severity, occurred_at DESC)
            WHERE severity IN ('warning', 'critical', 'fatal');
        CREATE INDEX idx_qm_health_workspace_time
            ON qm_account_health_events(workspace_id, occurred_at DESC);

        -- 不可篡改 trigger（复用 alembic 014 的模式）
        CREATE OR REPLACE FUNCTION qm_account_health_events_immutable() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'qm_account_health_events is append-only · UPDATE/DELETE blocked';
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER qm_health_no_update BEFORE UPDATE ON qm_account_health_events
            FOR EACH ROW EXECUTE FUNCTION qm_account_health_events_immutable();
        CREATE TRIGGER qm_health_no_delete BEFORE DELETE ON qm_account_health_events
            FOR EACH ROW EXECUTE FUNCTION qm_account_health_events_immutable();
        """
    )

    # ─── 7. 跨表外键 (browser_profile + proxy_pool → social_accounts) ─
    op.execute(
        """
        ALTER TABLE qm_social_accounts
            ADD CONSTRAINT fk_qm_social_browser
                FOREIGN KEY (browser_profile_id) REFERENCES qm_browser_profiles(id) ON DELETE SET NULL,
            ADD CONSTRAINT fk_qm_social_proxy
                FOREIGN KEY (proxy_pool_id) REFERENCES qm_proxy_pool(id) ON DELETE SET NULL;

        ALTER TABLE qm_account_health_events
            ADD CONSTRAINT fk_qm_health_proxy
                FOREIGN KEY (proxy_pool_id) REFERENCES qm_proxy_pool(id) ON DELETE SET NULL,
            ADD CONSTRAINT fk_qm_health_browser
                FOREIGN KEY (browser_profile_id) REFERENCES qm_browser_profiles(id) ON DELETE SET NULL;
        """
    )

    # ─── 8. 平台默认配额种子（合规上限参考 · 客户自行细调） ───────────
    op.execute(
        """
        -- 平台合规上限参考表 · 写进 qm_industry_templates 作为元数据
        INSERT INTO qm_industry_templates (slug, industry, name, description, template_type, template_json, is_featured)
        VALUES (
            'social-matrix-platform-quotas',
            'foreign_trade',
            '社媒平台合规配额参考',
            '6 大平台 / 12 子类别每日发帖 / 关注 / 点赞上限（2026 年标准·会随平台更新调整）',
            'sop',
            jsonb_build_object(
                'quotas', jsonb_build_array(
                    jsonb_build_object('platform', 'linkedin_company', 'daily_posts', 3, 'daily_follows', 0, 'daily_likes', 50),
                    jsonb_build_object('platform', 'linkedin_personal', 'daily_posts', 2, 'daily_follows', 100, 'daily_likes', 100),
                    jsonb_build_object('platform', 'tiktok_business', 'daily_posts', 5, 'daily_follows', 50, 'daily_likes', 200),
                    jsonb_build_object('platform', 'tiktok_creator', 'daily_posts', 10, 'daily_follows', 200, 'daily_likes', 500),
                    jsonb_build_object('platform', 'instagram_business', 'daily_posts', 5, 'daily_follows', 50, 'daily_likes', 150),
                    jsonb_build_object('platform', 'instagram_creator', 'daily_posts', 8, 'daily_follows', 100, 'daily_likes', 300),
                    jsonb_build_object('platform', 'facebook_page', 'daily_posts', 10, 'daily_follows', 0, 'daily_likes', 100),
                    jsonb_build_object('platform', 'x_twitter', 'daily_posts', 20, 'daily_follows', 50, 'daily_likes', 500),
                    jsonb_build_object('platform', 'youtube_channel', 'daily_posts', 2, 'daily_follows', 50, 'daily_likes', 100)
                )
            ),
            TRUE
        );
        """
    )

    # ─── 9. audit 留痕 ──────────────────────────────────────────────────
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
            '015_qm_social_matrix · 6 张账号管家表 + 平台合规配额种子',
            NULL, NULL,
            jsonb_build_object(
                'migration', '015_qm_social_matrix',
                'tables_added', 6,
                'actor_label', 'alembic_015_qm_social_matrix'
            )
        FROM tenants t WHERE t.slug = 'qide' LIMIT 1;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS qm_account_health_events CASCADE;")
    op.execute("DROP FUNCTION IF EXISTS qm_account_health_events_immutable() CASCADE;")
    op.execute("DROP TABLE IF EXISTS qm_post_schedules CASCADE;")
    op.execute("DROP TABLE IF EXISTS qm_social_posts CASCADE;")
    op.execute("DROP TABLE IF EXISTS qm_proxy_pool CASCADE;")
    op.execute("DROP TABLE IF EXISTS qm_browser_profiles CASCADE;")
    op.execute("DROP TABLE IF EXISTS qm_social_accounts CASCADE;")
