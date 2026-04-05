"""
GramGPT API — models/warmup.py
Модель задач прогрева аккаунтов.
По ТЗ раздел 3.4: имитация действий живого человека.
"""

from __future__ import annotations
from datetime import datetime
from typing import Optional
from sqlalchemy import String, Boolean, DateTime, ForeignKey, Integer, Text, JSON
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class WarmupTask(Base):
    __tablename__ = "warmup_tasks"

    id:             Mapped[int]              = mapped_column(Integer, primary_key=True)
    user_id:        Mapped[int]              = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    account_id:     Mapped[int]              = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True)

    # Режим: careful (осторожный), normal (обычный), aggressive (агрессивный)
    mode:           Mapped[str]              = mapped_column(String(32), default="normal")
    status:         Mapped[str]              = mapped_column(String(32), default="idle")  # idle | running | paused | finished

    # Какие действия выполнять
    read_feed:      Mapped[bool]             = mapped_column(Boolean, default=True)    # Чтение ленты
    view_stories:   Mapped[bool]             = mapped_column(Boolean, default=True)    # Просмотр Stories
    set_reactions:  Mapped[bool]             = mapped_column(Boolean, default=True)    # Реакции на посты
    join_channels:  Mapped[bool]             = mapped_column(Boolean, default=False)   # Вступление в каналы

    # Статистика
    actions_done:   Mapped[int]              = mapped_column(Integer, default=0)
    feeds_read:     Mapped[int]              = mapped_column(Integer, default=0)
    stories_viewed: Mapped[int]              = mapped_column(Integer, default=0)
    reactions_set:  Mapped[int]              = mapped_column(Integer, default=0)
    channels_joined:Mapped[int]              = mapped_column(Integer, default=0)

    # Метаданные
    started_at:     Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at:    Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at:     Mapped[datetime]           = mapped_column(DateTime, default=datetime.utcnow)
    updated_at:     Mapped[datetime]           = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    # v2 — расписание и дни
    day:             Mapped[int]              = mapped_column(Integer, default=1)
    day_started_at:  Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    today_actions:   Mapped[int]              = mapped_column(Integer, default=0)
    today_limit:     Mapped[int]              = mapped_column(Integer, default=5)
    is_resting:      Mapped[bool]             = mapped_column(Boolean, default=False)
    next_action_at:  Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    start_offset_min:Mapped[int]              = mapped_column(Integer, default=0)
    total_days:      Mapped[int]              = mapped_column(Integer, default=7)
    def __repr__(self):
        return f"<WarmupTask account={self.account_id} mode={self.mode} status={self.status}>"
