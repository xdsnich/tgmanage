"""
GramGPT API — models/parsed_channel.py
"""

from __future__ import annotations
from datetime import datetime
from typing import Optional
from sqlalchemy import String, Boolean, DateTime, ForeignKey, Integer, BigInteger
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class ParsedChannel(Base):
    __tablename__ = "parsed_channels"

    id:              Mapped[int]              = mapped_column(Integer, primary_key=True)
    user_id:         Mapped[int]              = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    channel_id:      Mapped[Optional[int]]    = mapped_column(BigInteger, nullable=True)
    username:        Mapped[str]              = mapped_column(String(128), default="", index=True)
    title:           Mapped[str]              = mapped_column(String(256), default="")
    subscribers:     Mapped[int]              = mapped_column(Integer, default=0)
    has_comments:    Mapped[bool]             = mapped_column(Boolean, default=False)
    last_post_date:  Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_verification: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    language:        Mapped[Optional[str]]    = mapped_column(String(10), nullable=True)
    search_query:    Mapped[str]              = mapped_column(String(256), default="")
    folder:          Mapped[str]              = mapped_column(String(128), default="", server_default="")
    added_at:        Mapped[datetime]         = mapped_column(DateTime, default=datetime.utcnow)