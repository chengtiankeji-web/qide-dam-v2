"""CRM 核心模块·Rocdesk 替代第一阶段·v7 MVP

业务逻辑层：
  classification.py    6 要素询盘分级算法（核心）
  leads_service.py     询盘 CRUD + 状态机
  contacts_service.py  联系人
  accounts_service.py  公司
  deals_service.py     商机
  quotes_service.py    报价单 + PDF 生成
  activities_service.py 活动 timeline

依赖：
  - sqlalchemy 2.0 async (db/session)
  - app.services.audit_service（审计每个状态变更）
  - app.services.ai_service（DashScope 起草回复 + 意图总结）
  - app.services.storage（PDF 写到 R2）
"""
