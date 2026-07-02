"""
models/account_error.py — Unified ошибки аккаунтов.

Любое место в коде где у конкретного TG-аккаунта что-то пошло не так
(прокси упал, FloodWait, session error, AUTH_KEY_UNREGISTERED,
PEER_FLOOD, BAN, ConnectionError, captcha и т.д.) — пишет сюда.

Для UI: можно фильтровать по account_id, source, error_type, времени.
"""
from __future__ import annotations
from datetime import datetime
from typing import Optional
from sqlalchemy import String, DateTime, ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from database import Base

# КРИТИЧНО: импортируем зависимые модели ДО объявления своей,
# иначе SQLAlchemy при create_all не найдёт telegram_accounts
# (NoReferencedTableError: account_error_logs.account_id → telegram_accounts).
from models import account as _acc_model  # noqa: F401
from models import user as _user_model    # noqa: F401


class AccountErrorLog(Base):
    __tablename__ = "account_error_logs"

    id:          Mapped[int]              = mapped_column(Integer, primary_key=True)
    account_id:  Mapped[int]              = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id:     Mapped[int]              = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    # Откуда ошибка: warmup | commenting | parser | bulk | tg_auth |
    # web_session | tdata | check | other. Расширяемо строкой.
    source:      Mapped[str]              = mapped_column(String(32), nullable=False, index=True)

    # Категория ошибки. По возможности — известная (FLOOD_WAIT,
    # AUTH_KEY_UNREGISTERED, ConnectionError, ProxyTimeout, PEER_FLOOD,
    # SessionRevoked, CaptchaNeeded, Unknown).
    error_type:  Mapped[str]              = mapped_column(String(64), nullable=False, index=True)

    # Краткое читаемое сообщение
    message:     Mapped[str]              = mapped_column(Text, default="")

    # Стектрейс/доп. контекст (опционально)
    context:     Mapped[Optional[str]]    = mapped_column(Text, nullable=True)

    # Доп. поля для удобства фильтрации
    proxy_id:    Mapped[Optional[int]]    = mapped_column(Integer, nullable=True)
    campaign_id: Mapped[Optional[int]]    = mapped_column(Integer, nullable=True, index=True)
    warmup_task_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)

    created_at:  Mapped[datetime]         = mapped_column(DateTime, default=datetime.utcnow, index=True)

    def __repr__(self):
        return f"<AccErr acc={self.account_id} {self.error_type} src={self.source}>"
