"""
GramGPT API — models/campaign_plan.py
План дня для аккаунта в кампании.
Генерируется при старте кампании, выполняется executor-ом.
"""

from __future__ import annotations
from datetime import datetime, date
from typing import Optional
from sqlalchemy import String, DateTime, Date, ForeignKey, Integer, JSON
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class CampaignPlan(Base):
    __tablename__ = "campaign_plans"

    id:              Mapped[int]              = mapped_column(Integer, primary_key=True)
    campaign_id:     Mapped[int]              = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True)
    warmup_task_id:  Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    account_id:      Mapped[int]              = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    plan_date:       Mapped[date]             = mapped_column(Date, nullable=False, index=True)
    day_number:      Mapped[int]              = mapped_column(Integer, default=1)
    plan:            Mapped[dict]             = mapped_column(JSON, nullable=False, default=dict)
    total_comments:  Mapped[int]              = mapped_column(Integer, default=0)
    executed_idx:    Mapped[int]              = mapped_column(Integer, default=0)
    status:          Mapped[str]              = mapped_column(String(32), default="active", index=True)
    created_at:      Mapped[datetime]         = mapped_column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<CampaignPlan campaign={self.campaign_id} account={self.account_id} day={self.day_number}>"
