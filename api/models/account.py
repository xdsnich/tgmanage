"""
GramGPT API — models/account.py
"""

from __future__ import annotations
from datetime import datetime
from typing import Optional, TYPE_CHECKING
from sqlalchemy import String, Boolean, DateTime, ForeignKey, Integer, BigInteger, Text, JSON, Enum
from sqlalchemy.orm import Mapped, mapped_column, relationship
import enum

from database import Base

if TYPE_CHECKING:
    from models.user import User
    from models.proxy import Proxy


class AccountStatus(str, enum.Enum):
    unknown    = "unknown"
    active     = "active"
    spamblock  = "spamblock"
    frozen     = "frozen"
    quarantine = "quarantine"
    error      = "error"


class AccountRole(str, enum.Enum):
    default    = "default"
    seller     = "продавец"
    warmer     = "прогреватель"
    reader     = "читатель"
    consultant = "консультант"


class TelegramAccount(Base):
    __tablename__ = "accounts"

    id:               Mapped[int]                  = mapped_column(Integer, primary_key=True)
    user_id:          Mapped[int]                  = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    phone:            Mapped[str]                  = mapped_column(String(20), nullable=False, index=True)
    tg_id:            Mapped[Optional[int]]        = mapped_column(BigInteger, nullable=True)
    first_name:       Mapped[str]                  = mapped_column(String(64), default="")
    last_name:        Mapped[str]                  = mapped_column(String(64), default="")
    username:         Mapped[str]                  = mapped_column(String(32), default="", index=True)
    bio:              Mapped[str]                  = mapped_column(String(70), default="")
    has_photo:        Mapped[bool]                 = mapped_column(Boolean, default=False)
    has_2fa:          Mapped[bool]                 = mapped_column(Boolean, default=False)
    active_sessions:  Mapped[int]                  = mapped_column(Integer, default=0)
    session_file:     Mapped[str]                  = mapped_column(String(512), default="")
    status:           Mapped[AccountStatus]        = mapped_column(Enum(AccountStatus), default=AccountStatus.unknown)
    trust_score:      Mapped[int]                  = mapped_column(Integer, default=0)
    role:             Mapped[AccountRole]          = mapped_column(Enum(AccountRole), default=AccountRole.default)
    tags:             Mapped[list]                 = mapped_column(JSON, default=list)
    notes:            Mapped[str]                  = mapped_column(Text, default="")
    channels:         Mapped[list]                 = mapped_column(JSON, default=list)
    quarantine_reason:Mapped[Optional[str]]        = mapped_column(String(255), nullable=True)
    quarantine_at:    Mapped[Optional[datetime]]   = mapped_column(DateTime, nullable=True)
    proxy_id:         Mapped[Optional[int]]        = mapped_column(ForeignKey("proxies.id"), nullable=True)
    added_at:         Mapped[datetime]             = mapped_column(DateTime, default=datetime.utcnow)
    last_checked:     Mapped[Optional[datetime]]   = mapped_column(DateTime, nullable=True)
    updated_at:       Mapped[datetime]             = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    error:            Mapped[Optional[str]]        = mapped_column(Text, nullable=True)

    user:  Mapped[User]           = relationship("User", back_populates="accounts")
    proxy: Mapped[Optional[Proxy]] = relationship("Proxy", back_populates="accounts")

    def __repr__(self):
        return f"<Account {self.phone} [{self.status}] trust={self.trust_score}>"