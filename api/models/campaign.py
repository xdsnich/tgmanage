"""
GramGPT API — models/campaign.py
Модели кампаний нейрокомментинга.
По ТЗ раздел 3.6: режимы, лимиты, промпты, тайминги.
"""

from __future__ import annotations
from datetime import datetime
from typing import Optional, TYPE_CHECKING
from sqlalchemy import (
    String, Boolean, DateTime, ForeignKey, Integer,
    BigInteger, Text, JSON, Float,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
import enum

from database import Base

if TYPE_CHECKING:
    from models.user import User
    from models.account import TelegramAccount


# ── Enums ────────────────────────────────────────────────────

class CampaignStatus(str, enum.Enum):
    draft    = "draft"       # Черновик — не запущена
    active   = "active"      # Работает
    paused   = "paused"      # На паузе
    stopped  = "stopped"     # Остановлена
    finished = "finished"    # Завершена (лимит/время)


class TriggerMode(str, enum.Enum):
    all      = "all"         # Каждый пост
    random   = "random"      # Случайный % постов
    keywords = "keywords"    # По ключевым словам


class LLMProvider(str, enum.Enum):
    claude = "claude"
    openai = "openai"
    gemini = "gemini"


class CommentTone(str, enum.Enum):
    positive   = "positive"     # Позитивный
    negative   = "negative"     # Негативный
    question   = "question"     # Вопрос автору
    analytical = "analytical"   # Аналитический
    short      = "short"        # Краткий (2-3 слова)
    custom     = "custom"       # Кастомный промпт


# ── Campaign ─────────────────────────────────────────────────

class Campaign(Base):
    __tablename__ = "campaigns"

    id:              Mapped[int]              = mapped_column(Integer, primary_key=True)
    user_id:         Mapped[int]              = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name:            Mapped[str]              = mapped_column(String(128), nullable=False)
    status:          Mapped[str]              = mapped_column(String(32), default="draft")

    # Какие аккаунты используются (JSON list of account IDs)
    account_ids:     Mapped[list]             = mapped_column(JSON, default=list)

    # ── Триггер ──────────────────────────────────────────
    trigger_mode:    Mapped[str]              = mapped_column(String(32), default="all")
    trigger_percent: Mapped[int]              = mapped_column(Integer, default=50)
    trigger_keywords:Mapped[list]             = mapped_column(JSON, default=list)

    # ── LLM ──────────────────────────────────────────────
    llm_provider:    Mapped[str]              = mapped_column(String(32), default="claude")
    tone:            Mapped[str]              = mapped_column(String(32), default="positive")
    custom_prompt:   Mapped[str]              = mapped_column(Text, default="")
    comment_length:  Mapped[str]              = mapped_column(String(32), default="medium")

    # ── Лимиты ───────────────────────────────────────────
    max_comments:    Mapped[int]              = mapped_column(Integer, default=100)
    max_hours:       Mapped[int]              = mapped_column(Integer, default=24)
    comments_sent:   Mapped[int]              = mapped_column(Integer, default=0)

    # ── Тайминги ─────────────────────────────────────────
    delay_join:      Mapped[int]              = mapped_column(Integer, default=10)
    delay_comment:   Mapped[int]              = mapped_column(Integer, default=250)           # Задержка перед комментом (сек)
    delay_between:   Mapped[int]              = mapped_column(Integer, default=60)            # Между комментами (сек)

    # ── Метаданные ───────────────────────────────────────
    started_at:      Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at:     Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at:      Mapped[datetime]           = mapped_column(DateTime, default=datetime.utcnow)
    updated_at:      Mapped[datetime]           = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # ── Relationships ────────────────────────────────────
    target_channels: Mapped[list[TargetChannel]] = relationship("TargetChannel", back_populates="campaign", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Campaign '{self.name}' [{self.status}] comments={self.comments_sent}/{self.max_comments}>"


# ── Target Channel ───────────────────────────────────────────

class TargetChannel(Base):
    __tablename__ = "target_channels"

    id:             Mapped[int]              = mapped_column(Integer, primary_key=True)
    campaign_id:    Mapped[int]              = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True)
    channel_id:     Mapped[Optional[int]]    = mapped_column(BigInteger, nullable=True)
    username:       Mapped[str]              = mapped_column(String(128), default="")
    title:          Mapped[str]              = mapped_column(String(256), default="")
    link:           Mapped[str]              = mapped_column(String(256), default="")
    subscribers:    Mapped[int]              = mapped_column(Integer, default=0)
    has_comments:   Mapped[bool]             = mapped_column(Boolean, default=True)
    last_post_id:   Mapped[int]              = mapped_column(Integer, default=0)        # ID последнего обработанного поста
    comments_sent:  Mapped[int]              = mapped_column(Integer, default=0)
    is_active:      Mapped[bool]             = mapped_column(Boolean, default=True)
    added_at:       Mapped[datetime]         = mapped_column(DateTime, default=datetime.utcnow)

    campaign: Mapped[Campaign] = relationship("Campaign", back_populates="target_channels")

    def __repr__(self):
        return f"<TargetChannel @{self.username} in campaign={self.campaign_id}>"


# ── Comment Log ──────────────────────────────────────────────

class CommentLog(Base):
    __tablename__ = "comment_logs"

    id:             Mapped[int]              = mapped_column(Integer, primary_key=True)
    campaign_id:    Mapped[int]              = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True)
    account_id:     Mapped[int]              = mapped_column(Integer, nullable=False)
    account_phone:  Mapped[str]              = mapped_column(String(32), default="")
    channel_username: Mapped[str]            = mapped_column(String(128), default="")
    channel_title:  Mapped[str]              = mapped_column(String(256), default="")
    post_id:        Mapped[int]              = mapped_column(Integer, default=0)
    post_text:      Mapped[str]              = mapped_column(Text, default="")
    comment_text:   Mapped[str]              = mapped_column(Text, default="")
    llm_provider:   Mapped[str]              = mapped_column(String(32), default="")
    created_at:     Mapped[datetime]         = mapped_column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<CommentLog campaign={self.campaign_id} @{self.channel_username}>"