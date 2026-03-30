"""
GramGPT API — models/actions_log.py
Журнал действий над аккаунтами.
По ТЗ раздел 3.3: actions_log — id, account_id, action_type, result, created_at
"""

from __future__ import annotations
from datetime import datetime
from typing import Optional, TYPE_CHECKING
from sqlalchemy import String, DateTime, ForeignKey, Integer, Text, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base

if TYPE_CHECKING:
    from models.account import TelegramAccount


class ActionLog(Base):
    __tablename__ = "actions_log"

    id:          Mapped[int]      = mapped_column(Integer, primary_key=True)
    account_id:  Mapped[int]      = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    action_type: Mapped[str]      = mapped_column(String(64), nullable=False, index=True)
    result:      Mapped[str]      = mapped_column(Text, default="")
    details:     Mapped[dict]     = mapped_column(JSON, default=dict)
    created_at:  Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    account: Mapped[TelegramAccount] = relationship("TelegramAccount", backref=None)

    def __repr__(self):
        return f"<ActionLog account={self.account_id} type={self.action_type} at={self.created_at}>"