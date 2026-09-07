"""GramGPT API — models/leomatch_onboarding.py
Массовый онбординг аккаунтов в @leomatchbot.

Task = один batch (юзер загрузил пул фото/городов/описаний, выбрал акки).
Item = один акк в этом batch'е (свой random-профиль).
"""

from __future__ import annotations
from datetime import datetime
from typing import Optional
from sqlalchemy import String, Boolean, DateTime, ForeignKey, Integer, Text, JSON
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class LeomatchOnboardingTask(Base):
    __tablename__ = "leomatch_onboarding_tasks"

    id:         Mapped[int]      = mapped_column(Integer, primary_key=True)
    user_id:    Mapped[int]      = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    label:      Mapped[str]      = mapped_column(String(128), default="")

    # Пулы (JSON-массивы, из которых random-выбор per-item)
    cities:        Mapped[list]  = mapped_column(JSON, default=list)
    descriptions:  Mapped[list]  = mapped_column(JSON, default=list)
    names:         Mapped[list]  = mapped_column(JSON, default=list)   # опционально
    photos:        Mapped[list]  = mapped_column(JSON, default=list)   # список путей к файлам

    # Общие параметры
    language:      Mapped[str]   = mapped_column(String(32), default="🇺🇦 Українська")
    age_min:       Mapped[int]   = mapped_column(Integer, default=22)
    age_max:       Mapped[int]   = mapped_column(Integer, default=32)
    # Что ищем: male | female | any
    seek:          Mapped[str]   = mapped_column(String(16), default="male")
    # Пол акка: auto (по имени) | male | female
    gender_mode:   Mapped[str]   = mapped_column(String(16), default="auto")
    # Сколько фото давать одному акку (random из пула)
    photos_per_acc: Mapped[int]  = mapped_column(Integer, default=1)

    # Прогресс
    status:        Mapped[str]      = mapped_column(String(32), default="pending")  # pending | running | done | error
    total_items:   Mapped[int]      = mapped_column(Integer, default=0)
    done_items:    Mapped[int]      = mapped_column(Integer, default=0)
    failed_items:  Mapped[int]      = mapped_column(Integer, default=0)
    error:         Mapped[str]      = mapped_column(Text, default="")

    created_at:    Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    started_at:    Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at:   Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    def __repr__(self):
        return f"<LeomatchOnboardingTask #{self.id} status={self.status} {self.done_items}/{self.total_items}>"


class LeomatchOnboardingItem(Base):
    __tablename__ = "leomatch_onboarding_items"

    id:         Mapped[int]      = mapped_column(Integer, primary_key=True)
    task_id:    Mapped[int]      = mapped_column(ForeignKey("leomatch_onboarding_tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    account_id: Mapped[int]      = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True)

    # Профиль для этого акка (посчитан при create)
    profile:    Mapped[dict]     = mapped_column(JSON, default=dict)
    # {name, city, description, age, gender, seek, photos:[paths]}

    status:     Mapped[str]      = mapped_column(String(32), default="pending")  # pending | running | done | error
    steps_log:  Mapped[list]     = mapped_column(JSON, default=list)  # [{step, bot_text, our_reply, ts}]
    error:      Mapped[str]      = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at:Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    def __repr__(self):
        return f"<LeomatchOnboardingItem #{self.id} task={self.task_id} acc={self.account_id} status={self.status}>"
