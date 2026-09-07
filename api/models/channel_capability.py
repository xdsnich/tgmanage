"""
GramGPT API — models/channel_capability.py

Глобальные (не per-account) свойства канала: есть ли обсуждения, нужна ли
заявка на вступление, разрешено ли писать обычному участнику.

Зачем: раньше это знание жило в Redis per-account
(`gramgpt:ch_forbidden:{acc_id}:{channel}`), поэтому каждый из 141 аккаунта
заново упирался в ту же стену и заново получал отказ от Telegram —
на 2026-08-22 накопилось 156 ключей ch_forbidden и 255 invite_pending.
Свойства канала от аккаунта не зависят: проверил один — знают все.

Заполняется ДВУМЯ read-only запросами (get_entity + GetFullChannelRequest),
без вступления в группу и без отправки сообщения. Замер 2026-08-22:
30 каналов подряд, 0 FLOOD_WAIT, ~0.2с на канал.
"""

from __future__ import annotations
from datetime import datetime
from typing import Optional

from sqlalchemy import String, Boolean, Integer, BigInteger, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class ChannelCapability(Base):
    __tablename__ = "channel_capabilities"

    id:               Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_username: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)

    has_discussion:   Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    discussion_id:    Mapped[Optional[int]]  = mapped_column(BigInteger, nullable=True)
    needs_approval:   Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    write_banned:     Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    # Надо ли вступить в discussion-группу перед первым комментом.
    # Канал при этом ПРИГОДЕН — исполнитель вступает сам (миграция 035).
    join_to_send:     Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)

    # Итоговый вердикт — можно ли вообще пытаться комментировать
    commentable:      Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, index=True)
    reason:           Mapped[Optional[str]]  = mapped_column(String(64), nullable=True)

    checked_at:         Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    checked_by_account: Mapped[Optional[int]]      = mapped_column(Integer, nullable=True)
    probe_source:       Mapped[Optional[str]]      = mapped_column(String(16), nullable=True)
    fail_count:         Mapped[int]                = mapped_column(Integer, default=0)
    last_error:         Mapped[Optional[str]]      = mapped_column(String(200), nullable=True)
    created_at:         Mapped[datetime]           = mapped_column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<ChannelCapability @{self.channel_username} commentable={self.commentable} ({self.reason})>"
