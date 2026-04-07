"""
GramGPT — tasks/behavior_engine.py
Behaviour Engine: персональные профили поведения для аккаунтов.

Каждый аккаунт получает детерминированный набор:
  - personality (lurker/active_reader/commenter/reactor/night_owl)
  - timing_profile (instant/fast/normal/careful/late/very_late)
  - style_profile (short_responder/thinker/questioner)

Определяется по hashlib.md5(phone) — один номер = всегда один профиль.
"""

import hashlib
import random
import logging

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
# PERSONALITY TEMPLATES
# ═══════════════════════════════════════════════════════════

PERSONALITY_TEMPLATES = [
    {
        "name": "lurker",
        "comment_chance": 0.15,
        "read_weight": 40,
        "reaction_weight": 15,
        "comment_delay_min": 600,
        "comment_delay_max": 3600,
        "session_count_min": 2,
        "session_count_max": 3,
        "typing_before_comment": True,
        "reads_before_comment_min": 3,
        "reads_before_comment_max": 8,
    },
    {
        "name": "active_reader",
        "comment_chance": 0.35,
        "read_weight": 30,
        "reaction_weight": 25,
        "comment_delay_min": 180,
        "comment_delay_max": 1200,
        "session_count_min": 4,
        "session_count_max": 6,
        "typing_before_comment": True,
        "reads_before_comment_min": 2,
        "reads_before_comment_max": 5,
    },
    {
        "name": "commenter",
        "comment_chance": 0.55,
        "read_weight": 20,
        "reaction_weight": 20,
        "comment_delay_min": 120,
        "comment_delay_max": 600,
        "session_count_min": 3,
        "session_count_max": 5,
        "typing_before_comment": True,
        "reads_before_comment_min": 1,
        "reads_before_comment_max": 3,
    },
    {
        "name": "reactor",
        "comment_chance": 0.10,
        "read_weight": 20,
        "reaction_weight": 40,
        "comment_delay_min": 900,
        "comment_delay_max": 5400,
        "session_count_min": 3,
        "session_count_max": 4,
        "typing_before_comment": False,
        "reads_before_comment_min": 1,
        "reads_before_comment_max": 2,
    },
    {
        "name": "night_owl",
        "comment_chance": 0.30,
        "read_weight": 25,
        "reaction_weight": 20,
        "comment_delay_min": 300,
        "comment_delay_max": 1800,
        "session_count_min": 2,
        "session_count_max": 4,
        "active_hours": (14, 3),
        "typing_before_comment": True,
        "reads_before_comment_min": 2,
        "reads_before_comment_max": 4,
    },
]


# ═══════════════════════════════════════════════════════════
# COMMENT TIMING PROFILES
# ═══════════════════════════════════════════════════════════

COMMENT_TIMING_PROFILES = [
    # 80% — пока пост горячий
    {"name": "instant",   "delay_min": 45,   "delay_max": 180,  "weight": 15},
    {"name": "fast",      "delay_min": 120,  "delay_max": 420,  "weight": 30},
    {"name": "normal",    "delay_min": 300,  "delay_max": 900,  "weight": 25},
    {"name": "careful",   "delay_min": 600,  "delay_max": 1800, "weight": 10},
    # 20% — поздние
    {"name": "late",      "delay_min": 1800, "delay_max": 3600, "weight": 12},
    {"name": "very_late", "delay_min": 3600, "delay_max": 7200, "weight": 8},
]


# ═══════════════════════════════════════════════════════════
# COMMENT STYLE PROFILES
# ═══════════════════════════════════════════════════════════

COMMENT_STYLE_PROFILES = [
    {
        "name": "short_responder",
        "length": "short",
        "uses_emoji": True,
        "starts_with_reply": False,
        "makes_typos": True,
        "asks_question": False,
    },
    {
        "name": "thinker",
        "length": "medium",
        "uses_emoji": False,
        "starts_with_reply": True,
        "makes_typos": False,
        "asks_question": False,
    },
    {
        "name": "questioner",
        "length": "medium",
        "uses_emoji": False,
        "starts_with_reply": False,
        "makes_typos": True,
        "asks_question": True,
    },
]


# ═══════════════════════════════════════════════════════════
# ACCOUNT LIMITS
# ═══════════════════════════════════════════════════════════

ACCOUNT_LIMITS = {
    "max_comments_per_day": 3,
    "max_comments_per_channel_day": 1,
    "cooldown_after_comment_min": 120,   # минуты (2 часа)
    "cooldown_after_comment_max": 360,   # минуты (6 часов)
    "min_account_age_days": 3,
}


# ═══════════════════════════════════════════════════════════
# DETERMINISTIC ASSIGNMENT FUNCTIONS
# ═══════════════════════════════════════════════════════════

def _phone_hash(phone: str) -> int:
    """Детерминированный хеш номера телефона."""
    return int(hashlib.md5(phone.encode()).hexdigest(), 16)


def assign_personality(phone: str) -> dict:
    """Всегда возвращает один и тот же personality для данного phone."""
    h = _phone_hash(phone)
    return PERSONALITY_TEMPLATES[h % len(PERSONALITY_TEMPLATES)]


def assign_timing_profile(phone: str) -> dict:
    """Выбирает timing profile с учётом весов, но детерминированно по phone."""
    h = _phone_hash(phone)
    total = sum(p["weight"] for p in COMMENT_TIMING_PROFILES)
    pick = h % total
    cumulative = 0
    for profile in COMMENT_TIMING_PROFILES:
        cumulative += profile["weight"]
        if pick < cumulative:
            return profile
    return COMMENT_TIMING_PROFILES[0]


def assign_style_profile(phone: str) -> dict:
    """Всегда возвращает один и тот же style для данного phone."""
    h = _phone_hash(phone)
    # Используем другой байт хеша чтобы не коррелировать с personality
    shifted = int(hashlib.md5((phone + "_style").encode()).hexdigest(), 16)
    return COMMENT_STYLE_PROFILES[shifted % len(COMMENT_STYLE_PROFILES)]


def get_comment_delay(timing_profile: dict) -> int:
    """Возвращает случайную задержку в секундах для данного timing profile."""
    return random.randint(timing_profile["delay_min"], timing_profile["delay_max"])


def get_full_profile(phone: str) -> dict:
    """Возвращает полный профиль поведения аккаунта."""
    personality = assign_personality(phone)
    timing = assign_timing_profile(phone)
    style = assign_style_profile(phone)
    return {
        "personality": personality,
        "timing_profile": timing,
        "style_profile": style,
    }


async def get_or_create_behavior(db, account_id: int, phone: str):
    """
    Получает или создаёт запись AccountBehavior для аккаунта.
    Personality/timing/style определяются детерминированно по phone.
    """
    from sqlalchemy import select
    from models.account_behavior import AccountBehavior
    from datetime import datetime

    result = await db.execute(
        select(AccountBehavior).where(AccountBehavior.account_id == account_id)
    )
    behavior = result.scalar_one_or_none()

    if behavior:
        # Сброс дневных счётчиков если нужно
        now = datetime.utcnow()
        if behavior.day_reset_at and (now - behavior.day_reset_at).total_seconds() >= 86400:
            behavior.comments_today = 0
            behavior.channels_commented_today = []
            behavior.day_reset_at = now
        elif not behavior.day_reset_at:
            behavior.day_reset_at = now
        return behavior

    profile = get_full_profile(phone)

    behavior = AccountBehavior(
        account_id=account_id,
        personality=profile["personality"]["name"],
        timing_profile=profile["timing_profile"]["name"],
        style_profile=profile["style_profile"],
        comments_today=0,
        channels_commented_today=[],
        day_reset_at=datetime.utcnow(),
    )
    db.add(behavior)
    await db.flush()

    logger.info(
        f"[behavior] Создан профиль для account#{account_id}: "
        f"personality={profile['personality']['name']}, "
        f"timing={profile['timing_profile']['name']}, "
        f"style={profile['style_profile']['name']}"
    )

    return behavior


def check_account_can_comment(behavior, channel: str) -> tuple[bool, str]:
    """
    Проверяет лимиты аккаунта. Возвращает (can_comment, reason).
    """
    from datetime import datetime

    now = datetime.utcnow()
    limits = ACCOUNT_LIMITS

    # Сброс дневных счётчиков
    if behavior.day_reset_at and (now - behavior.day_reset_at).total_seconds() >= 86400:
        behavior.comments_today = 0
        behavior.channels_commented_today = []
        behavior.day_reset_at = now

    # Max comments per day
    if behavior.comments_today >= limits["max_comments_per_day"]:
        return False, f"Дневной лимит: {behavior.comments_today}/{limits['max_comments_per_day']}"

    # Max per channel per day
    channels_today = behavior.channels_commented_today or []
    if channel in channels_today:
        return False, f"Уже комментировал @{channel} сегодня"

    # Cooldown
    if behavior.last_comment_at:
        elapsed_min = (now - behavior.last_comment_at).total_seconds() / 60
        cooldown = random.randint(limits["cooldown_after_comment_min"], limits["cooldown_after_comment_max"])
        if elapsed_min < cooldown:
            return False, f"Кулдаун: {int(elapsed_min)}/{cooldown} мин"

    return True, "ok"


def check_warmup_age(behavior) -> tuple[bool, str]:
    """Проверяет что аккаунт прогревается >= min_account_age_days."""
    from datetime import datetime

    if not behavior.created_at:
        return False, "Нет даты создания профиля"

    now = datetime.utcnow()
    age_days = (now - behavior.created_at).total_seconds() / 86400

    if age_days < ACCOUNT_LIMITS["min_account_age_days"]:
        return False, f"Прогрев: {age_days:.1f}/{ACCOUNT_LIMITS['min_account_age_days']} дней"

    return True, "ok"
