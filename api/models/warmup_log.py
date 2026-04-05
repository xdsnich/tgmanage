"""
GramGPT API — models/warmup_log.py
Детальные логи прогрева — каждое действие записывается.
"""

from __future__ import annotations
from datetime import datetime
from typing import Optional
from sqlalchemy import String, Boolean, DateTime, ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class WarmupLog(Base):
    __tablename__ = "warmup_logs"

    id:          Mapped[int]              = mapped_column(Integer, primary_key=True)
    task_id:     Mapped[int]              = mapped_column(ForeignKey("warmup_tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    account_id:  Mapped[int]              = mapped_column(Integer, nullable=False, index=True)
    action:      Mapped[str]              = mapped_column(String(64), nullable=False)
    detail:      Mapped[str]              = mapped_column(Text, default="")
    emoji:       Mapped[str]              = mapped_column(String(16), default="")
    channel:     Mapped[str]              = mapped_column(String(128), default="")
    success:     Mapped[bool]             = mapped_column(Boolean, default=True)
    error:       Mapped[Optional[str]]    = mapped_column(Text, nullable=True)
    created_at:  Mapped[datetime]         = mapped_column(DateTime, default=datetime.utcnow, index=True)

    def __repr__(self):
        return f"<WarmupLog task={self.task_id} action={self.action} ok={self.success}>"
