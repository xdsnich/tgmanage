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


class ProxyOut(BaseModel):
    id:           int
    host:         str
    port:         int
    login:        str
    protocol:     ProxyProtocol
    is_valid:     Optional[bool]
    last_checked: Optional[datetime]
    error:        Optional[str]
    created_at:   datetime

    model_config = {"from_attributes": True}


class ProxyBulkCreate(BaseModel):
    proxies_text: str  # Многострочный текст host:port:login:pass
