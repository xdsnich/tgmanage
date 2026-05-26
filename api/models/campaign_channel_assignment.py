"""
GramGPT API — models/campaign_channel_assignment.py
Матрица распределения: какой аккаунт подписывается на какой канал кампании.
"""

from datetime import datetime
from sqlalchemy import String, DateTime, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class CampaignChannelAssignment(Base):
    __tablename__ = "campaign_channel_assignments"

    id:               Mapped[int]      = mapped_column(Integer, primary_key=True)
    campaign_id:      Mapped[int]      = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True)
    account_id:       Mapped[int]      = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    channel_username: Mapped[str]      = mapped_column(String(255), nullable=False)

    # pending → joined | failed
    status:           Mapped[str]      = mapped_column(String(32), default="pending", index=True)

    # На какой день плана запланирована подписка
    planned_join_day: Mapped[int]      = mapped_column(Integer, nullable=True)

    assigned_at:      Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    joined_at:        Mapped[datetime] = mapped_column(DateTime, nullable=True)

    def __repr__(self):
        return (
            f"<CampaignChannelAssignment "
            f"camp={self.campaign_id} acc={self.account_id} "
            f"@{self.channel_username} [{self.status}]>"
        )
