"""
GramGPT API — models/account_behavior.py
Профиль поведения аккаунта — personality, timing, style.
Детерминированно присваивается по хешу phone.
"""

from __future__ import annotations
from datetime import datetime
from typing import Optional
from sqlalchemy import String, DateTime, ForeignKey, Integer, JSON
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class AccountBehavior(Base):
    __tablename__ = "account_behavior"

    id:                       Mapped[int]              = mapped_column(Integer, primary_key=True)
    account_id:               Mapped[int]              = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    personality:              Mapped[str]              = mapped_column(String(32), nullable=False)    # lurker/active_reader/commenter/reactor/night_owl
    timing_profile:           Mapped[str]              = mapped_column(String(32), nullable=False)    # instant/fast/normal/careful/late/very_late
    style_profile:            Mapped[dict]             = mapped_column(JSON, nullable=False)          # full style dict
    comments_today:           Mapped[int]              = mapped_column(Integer, default=0)
    last_comment_at:          Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    day_reset_at:             Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    channels_commented_today: Mapped[list]             = mapped_column(JSON, default=list)
    created_at:               Mapped[datetime]         = mapped_column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<AccountBehavior account={self.account_id} personality={self.personality}>"
