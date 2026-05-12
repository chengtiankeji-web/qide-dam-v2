"""CRM 核心 ORM models·与 alembic 009_crm_core 表对齐

各 model 与 v3 现有 models（asset / project / tenant / etc.）同风格：
  - SQLAlchemy 2.0 declarative
  - 复用 db.base.Base
  - 双向关系用 back_populates
  - 严格列定义（防 5/10 那个 setattr 不入库 bug）
"""
from app.models.crm.account import Account
from app.models.crm.contact import Contact
from app.models.crm.lead import Lead
from app.models.crm.deal import Deal
from app.models.crm.quote import Quote
from app.models.crm.activity import CRMActivity

__all__ = ["Account", "Contact", "Lead", "Deal", "Quote", "CRMActivity"]
