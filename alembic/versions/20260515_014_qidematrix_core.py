"""QideMatrix v0 核心 schema · workspaces + 订阅 + 邀请 + 工作流 + SSO

Revision ID: 014_qidematrix_core
Revises: 013_sha256_not_null
Create Date: 2026-05-15

═══════════════════════════════════════════════════════════════════════
背景：
═══════════════════════════════════════════════════════════════════════
QideMatrix 是 QideDAM 体系内的 SaaS 销售层（详见 handover/qingxuantech-strategic-research-2026-05-15.md）·
基础架构（多租户 / Vault / 审计 / LLM 路由）复用 QideDAM ·
QideMatrix 仅新增其特有的 10 张表（前缀 `qm_`）。

10 张新表：
  1. qm_workspaces            · 工作空间（订阅单元 / 计费单元）
  2. qm_workspace_members     · 工作空间成员
  3. qm_invitations           · 邀请（一次性 token）
  4. qm_subscriptions         · 订阅记录（计费台账）
  5. qm_billing_events        · 计费审计（不可改）
  6. qm_usage_meters          · 用量计量（按月）
  7. qm_workflows             · 工作流定义（自动化流程 · 区分 QideDAM 的审批工作流）
  8. qm_workflow_runs         · 工作流执行记录
  9. qm_industry_templates    · 行业模板库（系统级 · 跨 workspace 共享）
 10. qm_sso_sessions          · qingxuan → QideMatrix SSO bridge

═══════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "014_qidematrix_core"
down_revision: Union[str, None] = "013_sha256_not_null"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ─── 1. qm_workspaces ────────────────────────────────────────────
    op.execute(
        """
        CREATE TABLE qm_workspaces (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            slug VARCHAR(64) NOT NULL,
            display_name VARCHAR(200) NOT NULL,
            owner_user_id UUID REFERENCES users(id),
            plan VARCHAR(20) NOT NULL DEFAULT 'trial',
            plan_seats INT NOT NULL DEFAULT 3,
            plan_storage_gb INT NOT NULL DEFAULT 1,
            plan_ai_calls_monthly INT NOT NULL DEFAULT 100,
            trial_ends_at TIMESTAMPTZ,
            industry VARCHAR(50),
            locale VARCHAR(10) DEFAULT 'zh-CN',
            logo_url TEXT,
            primary_color VARCHAR(7),
            custom_domain VARCHAR(200),
            metadata JSONB DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            deleted_at TIMESTAMPTZ,
            CONSTRAINT uq_qm_workspaces_slug UNIQUE (slug),
            CONSTRAINT chk_qm_workspaces_plan
                CHECK (plan IN ('trial', 'standard', 'enterprise')),
            CONSTRAINT chk_qm_workspaces_slug_format
                CHECK (slug ~ '^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$')
        );
        CREATE INDEX idx_qm_workspaces_tenant_alive
            ON qm_workspaces(tenant_id) WHERE deleted_at IS NULL;
        CREATE INDEX idx_qm_workspaces_plan_alive
            ON qm_workspaces(plan) WHERE deleted_at IS NULL;
        CREATE INDEX idx_qm_workspaces_owner
            ON qm_workspaces(owner_user_id);
        """
    )

    # ─── 2. qm_workspace_members ─────────────────────────────────────
    op.execute(
        """
        CREATE TABLE qm_workspace_members (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            workspace_id UUID NOT NULL REFERENCES qm_workspaces(id) ON DELETE CASCADE,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            role VARCHAR(20) NOT NULL DEFAULT 'member',
            invited_by UUID REFERENCES users(id),
            joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_seen_at TIMESTAMPTZ,
            metadata JSONB DEFAULT '{}'::jsonb,
            CONSTRAINT uq_qm_member_workspace_user UNIQUE (workspace_id, user_id),
            CONSTRAINT chk_qm_member_role
                CHECK (role IN ('owner', 'admin', 'member', 'viewer'))
        );
        CREATE INDEX idx_qm_members_user ON qm_workspace_members(user_id);
        CREATE INDEX idx_qm_members_workspace_role ON qm_workspace_members(workspace_id, role);
        """
    )

    # ─── 3. qm_invitations ───────────────────────────────────────────
    op.execute(
        """
        CREATE TABLE qm_invitations (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            workspace_id UUID NOT NULL REFERENCES qm_workspaces(id) ON DELETE CASCADE,
            email VARCHAR(200) NOT NULL,
            role VARCHAR(20) NOT NULL DEFAULT 'member',
            invited_by_user_id UUID REFERENCES users(id),
            token VARCHAR(64) NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            accepted_at TIMESTAMPTZ,
            accepted_by_user_id UUID REFERENCES users(id),
            revoked_at TIMESTAMPTZ,
            metadata JSONB DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_qm_invitations_token UNIQUE (token),
            CONSTRAINT chk_qm_invitations_role
                CHECK (role IN ('admin', 'member', 'viewer'))
        );
        CREATE INDEX idx_qm_invitations_email_pending
            ON qm_invitations(email) WHERE accepted_at IS NULL AND revoked_at IS NULL;
        CREATE INDEX idx_qm_invitations_workspace
            ON qm_invitations(workspace_id);
        """
    )

    # ─── 4. qm_subscriptions ─────────────────────────────────────────
    op.execute(
        """
        CREATE TABLE qm_subscriptions (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            workspace_id UUID NOT NULL REFERENCES qm_workspaces(id) ON DELETE CASCADE,
            plan VARCHAR(20) NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'active',
            billing_cycle VARCHAR(10) NOT NULL DEFAULT 'monthly',
            price_cny_cents INT NOT NULL,
            started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            current_period_start TIMESTAMPTZ NOT NULL,
            current_period_end TIMESTAMPTZ NOT NULL,
            cancel_at_period_end BOOLEAN NOT NULL DEFAULT FALSE,
            cancelled_at TIMESTAMPTZ,
            payment_provider VARCHAR(20),
            payment_provider_subscription_id VARCHAR(200),
            metadata JSONB DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT chk_qm_subscriptions_plan
                CHECK (plan IN ('trial', 'standard', 'enterprise')),
            CONSTRAINT chk_qm_subscriptions_status
                CHECK (status IN ('active', 'past_due', 'cancelled', 'trial', 'expired')),
            CONSTRAINT chk_qm_subscriptions_billing_cycle
                CHECK (billing_cycle IN ('monthly', 'yearly')),
            CONSTRAINT chk_qm_subscriptions_provider
                CHECK (payment_provider IS NULL OR payment_provider IN ('wechat', 'stripe', 'manual'))
        );
        CREATE INDEX idx_qm_subs_workspace_status
            ON qm_subscriptions(workspace_id, status);
        CREATE INDEX idx_qm_subs_provider_external
            ON qm_subscriptions(payment_provider, payment_provider_subscription_id)
            WHERE payment_provider_subscription_id IS NOT NULL;
        CREATE INDEX idx_qm_subs_period_end_active
            ON qm_subscriptions(current_period_end) WHERE status = 'active';
        """
    )

    # ─── 5. qm_billing_events · 不可篡改（trigger 类似 audit_events） ──
    op.execute(
        """
        CREATE TABLE qm_billing_events (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            workspace_id UUID NOT NULL REFERENCES qm_workspaces(id),
            subscription_id UUID REFERENCES qm_subscriptions(id),
            event_type VARCHAR(40) NOT NULL,
            amount_cny_cents INT,
            payment_provider VARCHAR(20),
            payment_provider_event_id VARCHAR(200),
            actor_user_id UUID REFERENCES users(id),
            payload JSONB DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX idx_qm_billing_workspace_created
            ON qm_billing_events(workspace_id, created_at DESC);
        CREATE INDEX idx_qm_billing_event_type
            ON qm_billing_events(event_type);
        CREATE UNIQUE INDEX uq_qm_billing_provider_event
            ON qm_billing_events(payment_provider, payment_provider_event_id)
            WHERE payment_provider_event_id IS NOT NULL;

        -- 不可篡改 trigger（参考 alembic 004 audit_events）
        CREATE OR REPLACE FUNCTION qm_billing_events_immutable() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'qm_billing_events is append-only · UPDATE/DELETE blocked';
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER qm_billing_events_no_update BEFORE UPDATE ON qm_billing_events
            FOR EACH ROW EXECUTE FUNCTION qm_billing_events_immutable();
        CREATE TRIGGER qm_billing_events_no_delete BEFORE DELETE ON qm_billing_events
            FOR EACH ROW EXECUTE FUNCTION qm_billing_events_immutable();
        """
    )

    # ─── 6. qm_usage_meters · 按月 ────────────────────────────────────
    op.execute(
        """
        CREATE TABLE qm_usage_meters (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            workspace_id UUID NOT NULL REFERENCES qm_workspaces(id) ON DELETE CASCADE,
            period_month DATE NOT NULL,
            ai_calls_total INT NOT NULL DEFAULT 0,
            ai_tokens_input BIGINT NOT NULL DEFAULT 0,
            ai_tokens_output BIGINT NOT NULL DEFAULT 0,
            ai_cost_cny_cents INT NOT NULL DEFAULT 0,
            storage_bytes BIGINT NOT NULL DEFAULT 0,
            active_users INT NOT NULL DEFAULT 0,
            workflow_runs INT NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_qm_usage_workspace_period UNIQUE (workspace_id, period_month)
        );
        CREATE INDEX idx_qm_usage_period
            ON qm_usage_meters(period_month, workspace_id);
        """
    )

    # ─── 7. qm_workflows + qm_workflow_runs ───────────────────────────
    op.execute(
        """
        CREATE TABLE qm_workflows (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            workspace_id UUID NOT NULL REFERENCES qm_workspaces(id) ON DELETE CASCADE,
            name VARCHAR(200) NOT NULL,
            description TEXT,
            trigger_type VARCHAR(40) NOT NULL,
            trigger_config JSONB NOT NULL DEFAULT '{}'::jsonb,
            steps_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            status VARCHAR(20) NOT NULL DEFAULT 'draft',
            template_slug VARCHAR(100),
            created_by_user_id UUID REFERENCES users(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            deleted_at TIMESTAMPTZ,
            CONSTRAINT chk_qm_workflows_status
                CHECK (status IN ('draft', 'active', 'paused', 'archived')),
            CONSTRAINT chk_qm_workflows_trigger
                CHECK (trigger_type IN ('manual', 'schedule', 'webhook', 'event'))
        );
        CREATE INDEX idx_qm_workflows_workspace_alive
            ON qm_workflows(workspace_id) WHERE deleted_at IS NULL;
        CREATE INDEX idx_qm_workflows_template
            ON qm_workflows(template_slug) WHERE template_slug IS NOT NULL;

        CREATE TABLE qm_workflow_runs (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            workflow_id UUID NOT NULL REFERENCES qm_workflows(id) ON DELETE CASCADE,
            workspace_id UUID NOT NULL REFERENCES qm_workspaces(id),
            triggered_by VARCHAR(40),
            triggered_by_user_id UUID REFERENCES users(id),
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            input_data JSONB,
            output_data JSONB,
            step_results JSONB NOT NULL DEFAULT '[]'::jsonb,
            error TEXT,
            started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            completed_at TIMESTAMPTZ,
            duration_ms INT,
            CONSTRAINT chk_qm_runs_status
                CHECK (status IN ('pending', 'running', 'completed', 'failed', 'cancelled'))
        );
        CREATE INDEX idx_qm_runs_workflow_started
            ON qm_workflow_runs(workflow_id, started_at DESC);
        CREATE INDEX idx_qm_runs_pending_status
            ON qm_workflow_runs(status) WHERE status IN ('pending', 'running');
        """
    )

    # ─── 8. qm_industry_templates · 系统级共享 ────────────────────────
    op.execute(
        """
        CREATE TABLE qm_industry_templates (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            slug VARCHAR(100) NOT NULL UNIQUE,
            industry VARCHAR(50) NOT NULL,
            name VARCHAR(200) NOT NULL,
            description TEXT,
            template_type VARCHAR(40) NOT NULL,
            template_json JSONB NOT NULL,
            preview_image_url TEXT,
            is_featured BOOLEAN NOT NULL DEFAULT FALSE,
            install_count INT NOT NULL DEFAULT 0,
            metadata JSONB DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT chk_qm_templates_type
                CHECK (template_type IN ('workflow', 'sop', 'prompt', 'dashboard'))
        );
        CREATE INDEX idx_qm_templates_industry_type
            ON qm_industry_templates(industry, template_type);
        CREATE INDEX idx_qm_templates_featured
            ON qm_industry_templates(is_featured, install_count DESC) WHERE is_featured = TRUE;
        """
    )

    # ─── 9. qm_sso_sessions ──────────────────────────────────────────
    op.execute(
        """
        CREATE TABLE qm_sso_sessions (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            bridge_token VARCHAR(64) NOT NULL UNIQUE,
            target_workspace_id UUID REFERENCES qm_workspaces(id),
            issued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMPTZ NOT NULL,
            used_at TIMESTAMPTZ,
            ip VARCHAR(45),
            user_agent TEXT
        );
        CREATE INDEX idx_qm_sso_token_unused
            ON qm_sso_sessions(bridge_token) WHERE used_at IS NULL;
        CREATE INDEX idx_qm_sso_user
            ON qm_sso_sessions(user_id, issued_at DESC);
        """
    )

    # ─── 10. 种子数据 · 3 个行业模板 + 试用价格 ─────────────────────
    op.execute(
        """
        INSERT INTO qm_industry_templates (slug, industry, name, description, template_type, template_json, is_featured)
        VALUES
        (
            'foreign-trade-inquiry-pipeline',
            'foreign_trade',
            '外贸询盘自动分级 + 跟进',
            '客户询盘到 → AI 6 要素自动分级 (A/B/C/D) → 派单给销售 → 24h 跟进提醒 → 报价单生成',
            'workflow',
            jsonb_build_object(
                'steps', jsonb_build_array(
                    jsonb_build_object('type', 'classify_inquiry', 'use_case', 'lead_classify'),
                    jsonb_build_object('type', 'assign_to_sales', 'rule', 'round_robin'),
                    jsonb_build_object('type', 'remind_followup_24h'),
                    jsonb_build_object('type', 'draft_quote', 'use_case', 'intake_extract')
                )
            ),
            TRUE
        ),
        (
            'manufacturer-content-matrix-monthly',
            'manufacturing',
            '工厂海外内容矩阵 · 月产 100+',
            '基于工厂数据 → 每周生成 6 平台内容 (LinkedIn / TikTok / Instagram / FB / X / YouTube Shorts) → 自动发布 → 数据回流',
            'workflow',
            jsonb_build_object(
                'steps', jsonb_build_array(
                    jsonb_build_object('type', 'gather_product_data'),
                    jsonb_build_object('type', 'generate_seo_article', 'use_case', 'seo_writer'),
                    jsonb_build_object('type', 'rewrite_for_platforms', 'use_case', 'translate_rewrite'),
                    jsonb_build_object('type', 'schedule_publish'),
                    jsonb_build_object('type', 'collect_metrics')
                )
            ),
            TRUE
        ),
        (
            'content-service-client-onboarding',
            'content',
            '内容服务客户接入 SOP',
            '新客户签约 → 自动建项目空间 → 邀请协作 → 模板初始化 → 第一次内容产出',
            'workflow',
            jsonb_build_object(
                'steps', jsonb_build_array(
                    jsonb_build_object('type', 'create_workspace'),
                    jsonb_build_object('type', 'invite_team'),
                    jsonb_build_object('type', 'init_templates'),
                    jsonb_build_object('type', 'first_deliverable')
                )
            ),
            FALSE
        );
        """
    )

    # ─── 11. 写 audit ────────────────────────────────────────────────
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
            '014_qidematrix_core · 10 张 qm_* 表 + 3 个行业模板种子数据落地',
            NULL, NULL,
            jsonb_build_object(
                'migration', '014_qidematrix_core',
                'tables_added', 10,
                'templates_seeded', 3,
                'actor_label', 'alembic_014_qidematrix'
            )
        FROM tenants t WHERE t.slug = 'qide' LIMIT 1;
        """
    )


def downgrade() -> None:
    """全删（清空 SaaS 业务数据 · 谨慎调用）"""
    op.execute("DROP TABLE IF EXISTS qm_sso_sessions CASCADE;")
    op.execute("DROP TABLE IF EXISTS qm_industry_templates CASCADE;")
    op.execute("DROP TABLE IF EXISTS qm_workflow_runs CASCADE;")
    op.execute("DROP TABLE IF EXISTS qm_workflows CASCADE;")
    op.execute("DROP TABLE IF EXISTS qm_usage_meters CASCADE;")
    op.execute("DROP TABLE IF EXISTS qm_billing_events CASCADE;")
    op.execute("DROP FUNCTION IF EXISTS qm_billing_events_immutable() CASCADE;")
    op.execute("DROP TABLE IF EXISTS qm_subscriptions CASCADE;")
    op.execute("DROP TABLE IF EXISTS qm_invitations CASCADE;")
    op.execute("DROP TABLE IF EXISTS qm_workspace_members CASCADE;")
    op.execute("DROP TABLE IF EXISTS qm_workspaces CASCADE;")
