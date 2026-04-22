"""
GramGPT API — schemas/proxy.py
"""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel

from models.proxy import ProxyProtocol


class ProxyCreate(BaseModel):
    host:     str
    port:     int
    login:    str = ""
    password: str = ""
    protocol: ProxyProtocol = ProxyProtocol.socks5
    country:  str = ""
    city:     str = ""
    # Срок действия (устанавливается относительно "сейчас")
    # Если оба 0 — прокси бессрочный
    duration_days:  int = 0
    duration_hours: int = 0


class ProxyOut(BaseModel):
    id:           int
    host:         str
    port:         int
    login:        str
    protocol:     ProxyProtocol
    is_valid:     Optional[bool]
    last_checked: Optional[datetime]
    error:        Optional[str]
    country:      str = ""
    country_code: str = ""
    city:         str = ""
    expires_at:   Optional[datetime] = None
    created_at:   datetime

    model_config = {"from_attributes": True}


class ProxyBulkCreate(BaseModel):
    proxies_text:   str  # Многострочный текст host:port:login:pass
    duration_days:  int = 0   # Для всех создаваемых прокси
    duration_hours: int = 0