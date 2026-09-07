from __future__ import annotations
from datetime import datetime
from typing import Optional
from sqlalchemy import String, Integer, Text, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from database import Base


class ChannelBanStats(Base):
    __tablename__ = "channel_ban_stats"
    __table_args__ = (UniqueConstraint("user_id", "channel_username"),)

    id:                 Mapped[int]              = mapped_column(Integer, primary_key=True)
    user_id:            Mapped[int]              = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    channel_username:   Mapped[str]              = mapped_column(String(128), nullable=False, index=True)
    total_attempts:     Mapped[int]              = mapped_column(Integer, default=0)
    banned_count:       Mapped[int]              = mapped_column(Integer, default=0)
    # Комменты которые НЕ дошли до отправки (canWrite=false, no discussion,
    # MsgIdInvalid и т.д.). Если растёт — системная проблема с каналом.
    skipped_count:      Mapped[int]              = mapped_column(Integer, default=0)
    last_ban_reason:    Mapped[Optional[str]]    = mapped_column(Text, nullable=True)
    last_skip_reason:   Mapped[Optional[str]]    = mapped_column(Text, nullable=True)
    last_updated:       Mapped[datetime]         = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_at:         Mapped[datetime]         = mapped_column(DateTime, default=datetime.utcnow)

    @property
    def pass_rate(self) -> float:
        """Процент реально ДОШЕДШИХ комментов от всех попыток.
        skipped_count тоже вычитаем — это неотправленные (не ошибка,
        но и не успех)."""
        if self.total_attempts == 0:
            return 100.0
        _ok = self.total_attempts - (self.banned_count or 0) - (self.skipped_count or 0)
        return round(max(0, _ok) / self.total_attempts * 100, 1)