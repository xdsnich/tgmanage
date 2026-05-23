"""
GramGPT API — models/account_subscription.py
Глобальний реєстр підписок акаунта (Anti-Ban).
"""

from datetime import datetime
from sqlalchemy import String, DateTime, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column

from database import Base

class AccountSubscription(Base):
    __tablename__ = "account_subscriptions"

    id:               Mapped[int]      = mapped_column(Integer, primary_key=True)
    account_id:       Mapped[int]      = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    channel_username: Mapped[str]      = mapped_column(String(255), nullable=False, index=True)
    
    # Статус: 'active' (підписані), 'left' (вийшли), 'banned' (забанили в каналі)
    status:           Mapped[str]      = mapped_column(String(32), default="active", index=True)
    joined_at:        Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<AccountSubscription acc={self.account_id} channel={self.channel_username} status={self.status}>"