"""
GramGPT API — models/parser_event.py
События парсера для метрик и статистики.
"""

from __future__ import annotations
from datetime import datetime
from typing import Optional
from sqlalchemy import String, Integer, Text, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from database import Base


class ParserEvent(Base):
    __tablename__ = "parser_events"

    id:              Mapped[int]              = mapped_column(Integer, primary_key=True)
    user_id:         Mapped[int]              = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    account_id:      Mapped[Optional[int]]    = mapped_column(Integer, nullable=True)

    # Тип события
    event_type:      Mapped[str]              = mapped_column(String(32), nullable=False)
    # 'flood_wait'     — словили FLOOD_WAIT
    # 'session_start'  — начало парсинг-сессии
    # 'session_done'   — конец сессии (успешный или прерванный)
    # 'error'          — критическая ошибка

    source:          Mapped[Optional[str]]    = mapped_column(String(32), nullable=True)
    # 'similar' | 'search' | 'verify' | 'import'

    wait_seconds:    Mapped[int]              = mapped_column(Integer, default=0)    # для flood_wait
    channels_found:  Mapped[int]              = mapped_column(Integer, default=0)    # для session_done
    channels_saved:  Mapped[int]              = mapped_column(Integer, default=0)    # для session_done
    duration_sec:    Mapped[int]              = mapped_column(Integer, default=0)    # длительность сессии

    seed:            Mapped[Optional[str]]    = mapped_column(String(256), nullable=True)  # для crawler
    details:         Mapped[Optional[str]]    = mapped_column(Text, nullable=True)          # произвольные данные

    created_at:      Mapped[datetime]         = mapped_column(DateTime, default=datetime.utcnow, index=True)

    def __repr__(self):
        return f"<ParserEvent {self.event_type}/{self.source} user={self.user_id}>"
