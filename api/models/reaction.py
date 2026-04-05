"""
GramGPT API — models/reaction.py
Задачи на реакции к постам и комментариям в каналах.
"""

from __future__ import annotations
from datetime import datetime
from typing import Optional
from sqlalchemy import String, Boolean, DateTime, ForeignKey, Integer, Text, JSON
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class ReactionTask(Base):
    __tablename__ = "reaction_tasks"

    id:              Mapped[int]              = mapped_column(Integer, primary_key=True)
    user_id:         Mapped[int]              = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    # Что реактим
    channel_link:    Mapped[str]              = mapped_column(String(256), nullable=False)
    post_id:         Mapped[Optional[int]]    = mapped_column(Integer, nullable=True)

    # Куда ставить реакции
    target:          Mapped[str]              = mapped_column(String(32), default="post")
    # post = на сам пост
    # comments = на комментарии под постом
    # both = и на пост, и на комментарии

    comments_limit:  Mapped[int]              = mapped_column(Integer, default=5)
    # Сколько комментариев реактить (последние N)

    # Какие аккаунты
    account_ids:     Mapped[list]             = mapped_column(JSON, default=list)

    # Какие реакции
    reactions:       Mapped[list]             = mapped_column(JSON, default=list)
    mode:            Mapped[str]              = mapped_column(String(32), default="random")

    # Настройки
    count:           Mapped[int]              = mapped_column(Integer, default=0)
    delay_min:       Mapped[int]              = mapped_column(Integer, default=3)
    delay_max:       Mapped[int]              = mapped_column(Integer, default=15)

    # Статус
    status:          Mapped[str]              = mapped_column(String(32), default="pending")
    reactions_sent:  Mapped[int]              = mapped_column(Integer, default=0)
    reactions_failed:Mapped[int]              = mapped_column(Integer, default=0)
    error:           Mapped[Optional[str]]    = mapped_column(Text, nullable=True)
    results:         Mapped[list]             = mapped_column(JSON, default=list)

    started_at:      Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at:     Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at:      Mapped[datetime]           = mapped_column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<ReactionTask channel={self.channel_link} target={self.target} status={self.status}>"