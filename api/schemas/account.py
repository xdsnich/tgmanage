"""
GramGPT API — schemas/account.py
"""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel

from models.account import AccountStatus, AccountRole


class AccountCreate(BaseModel):
    phone: str


class AccountUpdate(BaseModel):
    first_name: Optional[str] = None
    last_name:  Optional[str] = None
    bio:        Optional[str] = None
    role:       Optional[AccountRole] = None
    tags:       Optional[list[str]]   = None
    notes:      Optional[str] = None
    proxy_id:   Optional[int] = None
    geo:        Optional[str] = None
    category:   Optional[str] = None


class AccountOut(BaseModel):
    id:              int
    phone:           str
    tg_id:           Optional[int]
    first_name:      str
    last_name:       str
    username:        str
    bio:             str
    has_photo:       bool
    has_2fa:         bool
    active_sessions: int
    session_file:    str
    status:          AccountStatus
    trust_score:     int
    role:            AccountRole
    tags:            list
    notes:           str
    channels:        list
    proxy_id:        Optional[int]
    geo:             str = ""
    category:        str = ""
    added_at:        datetime
    last_checked:    Optional[datetime]
    error:           Optional[str]
    api_app_id:  Optional[int] = None

    model_config = {"from_attributes": True}


class AccountCheckResult(BaseModel):
    phone:        str
    status:       AccountStatus
    trust_score:  int
    last_checked: datetime
    error:        Optional[str] = None