"""
GramGPT — trust.py
Расчёт Trust Score аккаунта
Отвечает за: подсчёт баллов, рекомендации по улучшению
"""

from config import TRUST_SCORE


def calculate(account: dict) -> int:
    """Считает Trust Score от 0 до 100"""
    score = TRUST_SCORE["base"]

    if account.get("username"):
        score += TRUST_SCORE["has_username"]
    if account.get("bio"):
        score += TRUST_SCORE["has_bio"]
    if account.get("has_photo"):
        score += TRUST_SCORE["has_photo"]
    if account.get("active_sessions", 0) > 0:
        score += TRUST_SCORE["active_ok"]
    if account.get("status") == "spamblock":
        score += TRUST_SCORE["spamblock"]
    if account.get("status") == "frozen":
        score += TRUST_SCORE["frozen"]

    return max(0, min(100, score))


def get_recommendations(account: dict) -> list[str]:
    """Возвращает список советов как поднять Trust Score"""
    tips = []

    if not account.get("username"):
        tips.append(f"+{TRUST_SCORE['has_username']} баллов — добавь username аккаунту")
    if not account.get("bio"):
        tips.append(f"+{TRUST_SCORE['has_bio']} балла  — заполни описание профиля (bio)")
    if not account.get("has_photo"):
        tips.append(f"+{TRUST_SCORE['has_photo']} балла  — загрузи фото профиля")
    if account.get("status") == "spamblock":
        tips.append("⚠️  Снять спамблок: подожди 24ч и напиши в @SpamBot")
    if account.get("status") == "frozen":
        tips.append("⚠️  Аккаунт заморожен — требуется переавторизация")

    return tips


def get_grade(score: int) -> str:
    """Текстовая оценка по баллу"""
    if score >= 80:
        return "Отличный"
    elif score >= 60:
        return "Хороший"
    elif score >= 40:
        return "Средний"
    elif score >= 20:
        return "Слабый"
    else:
        return "Критический"
