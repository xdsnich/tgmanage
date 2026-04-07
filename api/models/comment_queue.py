"""
GramGPT API — models/comment_queue.py
Очередь комментариев v2 — комментарии ставятся в очередь с scheduled_at,
а не отправляются сразу.
"""

from __future__ import annotations
from datetime import datetime
from typing import Optional
from sqlalchemy import String, DateTime, ForeignKey, Integer, Text, JSON
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class CommentQueue(Base):
    __tablename__ = "comment_queue"

    id:             Mapped[int]              = mapped_column(Integer, primary_key=True)
    campaign_id:    Mapped[int]              = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True)
    account_id:     Mapped[int]              = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    channel:        Mapped[str]              = mapped_column(String(128), nullable=False)
    post_id:        Mapped[int]              = mapped_column(Integer, nullable=False)
    post_text:      Mapped[str]              = mapped_column(Text, default="")
    personality:    Mapped[dict]             = mapped_column(JSON, default=dict)
    style:          Mapped[dict]             = mapped_column(JSON, default=dict)
    status:         Mapped[str]              = mapped_column(String(32), default="scheduled", index=True)  # scheduled | executing | done | failed | aborted
    scheduled_at:   Mapped[datetime]         = mapped_column(DateTime, nullable=False, index=True)
    executed_at:    Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    comment_text:   Mapped[Optional[str]]    = mapped_column(Text, nullable=True)
    error:          Mapped[Optional[str]]    = mapped_column(Text, nullable=True)
    created_at:     Mapped[datetime]         = mapped_column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<CommentQueue #{self.id} @{self.channel} [{self.status}] scheduled={self.scheduled_at}>"
