"""
models/tgstat_checked_channel.py — реестр «уже проверенных на TGStat».

Зачем: юзер спарсил 300 каналов, у 30 нашлись комменты (сохранены
в ParsedChannel). У остальных 270 — комментов нет, но мы уже
потратили прокси-cooldown на их проверку. При следующем запуске
без этой таблицы мы бы ПОВТОРНО проверяли те же 270 из топа
рейтинга + догоняли новые.

С этой таблицей: перед Stage 2 отсекаем всех кто уже был проверен
за последние N дней. Stage 2 идёт только по новым — экономия
прокси-времени.
"""
from __future__ import annotations
from datetime import datetime
from typing import Optional
from sqlalchemy import String, DateTime, ForeignKey, Integer, Boolean, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from database import Base

# Регистрируем FK-таргеты до объявления класса — иначе NoReferencedTable
# при create_all (та же ошибка что была у account_error_logs).
from models import user as _user_model  # noqa: F401


class TgstatCheckedChannel(Base):
    __tablename__ = "tgstat_checked_channels"

    id:            Mapped[int]              = mapped_column(Integer, primary_key=True)
    user_id:       Mapped[int]              = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    username:      Mapped[str]              = mapped_column(String(128), nullable=False, index=True)
    has_comments:  Mapped[bool]             = mapped_column(Boolean, default=False)
    linked_chat:   Mapped[Optional[str]]    = mapped_column(String(128), nullable=True)
    subscribers:   Mapped[Optional[int]]    = mapped_column(Integer, nullable=True)
    title:         Mapped[Optional[str]]    = mapped_column(String(256), nullable=True)
    # job_id последней проверки — для дебага и очистки конкретных прогонов
    last_job_id:   Mapped[Optional[str]]    = mapped_column(String(64), nullable=True)
    checked_at:    Mapped[datetime]         = mapped_column(DateTime, default=datetime.utcnow, index=True)

    # Один канал у одного юзера — одна запись (свежая перезаписывает)
    __table_args__ = (
        UniqueConstraint("user_id", "username", name="ux_tgstat_checked_user_username"),
    )
