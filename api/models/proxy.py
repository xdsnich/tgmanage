"""
GramGPT API — models/proxy.py
"""

from __future__ import annotations
from datetime import datetime
from typing import Optional, TYPE_CHECKING
from sqlalchemy import String, Boolean, DateTime, ForeignKey, Integer, Enum
from sqlalchemy.orm import Mapped, mapped_column, relationship
import enum

from database import Base

if TYPE_CHECKING:
    from models.user import User
    from models.account import TelegramAccount


class ProxyProtocol(str, enum.Enum):
    socks5 = "socks5"
    http   = "http"


class Proxy(Base):
    __tablename__ = "proxies"

    id:           Mapped[int]             = mapped_column(Integer, primary_key=True)
    user_id:      Mapped[int]             = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    host:         Mapped[str]             = mapped_column(String(255), nullable=False)
    port:         Mapped[int]             = mapped_column(Integer, nullable=False)
    login:        Mapped[str]             = mapped_column(String(128), default="")
    password:     Mapped[str]             = mapped_column(String(128), default="")
    protocol:     Mapped[ProxyProtocol]   = mapped_column(Enum(ProxyProtocol), default=ProxyProtocol.socks5)
    is_valid:     Mapped[Optional[bool]]  = mapped_column(Boolean, nullable=True)
    last_checked: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    error:        Mapped[Optional[str]]   = mapped_column(String(255), nullable=True)
    created_at:   Mapped[datetime]        = mapped_column(DateTime, default=datetime.utcnow)

    user:     Mapped[User]                  = relationship("User", back_populates="proxies")
    accounts: Mapped[list[TelegramAccount]] = relationship("TelegramAccount", back_populates="proxy")

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    def __repr__(self):
        return f"<Proxy {self.host}:{self.port} [{self.protocol}]>"