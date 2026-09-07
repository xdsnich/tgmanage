"""
GramGPT API — models/account_subscription.py
Глобальный реестр подписок акка (anti-ban + переиспользование).

Заполняется автоматически при успешных join_target_channel /
join_channel / join_discussion_of / react_to_comment / smart_comment.
Используется caller'ом кампаний: если акк уже подписан — не ставим
join_target_channel action в план (экономия API + меньше риска бана).

Правило "готов комментить" (юзер настоял 2026-07-16):
    is_channel_joined OR is_discussion_joined == True
"""

from datetime import datetime
from typing import Optional
from sqlalchemy import String, DateTime, ForeignKey, Integer, BigInteger, Boolean, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class AccountSubscription(Base):
    __tablename__ = "account_subscriptions"
    __table_args__ = (UniqueConstraint("account_id", "channel_username"),)

    id:                   Mapped[int]              = mapped_column(Integer, primary_key=True)
    account_id:           Mapped[int]              = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    channel_username:     Mapped[str]              = mapped_column(String(255), nullable=False, index=True)

    # ── Основные флаги (расширение 2026-07-16, migration 032) ──
    channel_id:           Mapped[Optional[int]]    = mapped_column(BigInteger, nullable=True)
    is_channel_joined:    Mapped[bool]             = mapped_column(Boolean, default=False)
    is_discussion_joined: Mapped[bool]             = mapped_column(Boolean, default=False)
    # 'campaign' | 'warmup' | 'decoy' | 'import' | 'api_check' | 'react' | 'comment' | 'join_target' | 'join_discussion'
    discovered_via:       Mapped[str]              = mapped_column(String(32), default="")

    # ── Legacy поля (сохранены для совместимости) ──
    # Статус: 'active' | 'left' | 'banned'
    status:               Mapped[str]              = mapped_column(String(32), default="active", index=True)
    joined_at:            Mapped[datetime]         = mapped_column(DateTime, default=datetime.utcnow)
    last_verified_at:     Mapped[datetime]         = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        flags = []
        if self.is_channel_joined:    flags.append("CH")
        if self.is_discussion_joined: flags.append("DISC")
        return f"<AccountSubscription acc={self.account_id} @{self.channel_username} [{'+'.join(flags) or self.status}]>"