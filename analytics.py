"""
GramGPT — analytics.py
Аналитика и мониторинг аккаунтов
По ТЗ раздел 4:
  - Health Dashboard — сводка по всем аккаунтам
  - Trust Score статистика
  - Фильтрация и поиск
  - Рекомендации
"""

from datetime import datetime, timedelta
from pathlib import Path

import config
import ui
import trust as trust_module


# ============================================================
# HEALTH DASHBOARD — главная сводка
# ============================================================

def health_dashboard(accounts: list[dict]):
    """
    Выводит полный дашборд состояния всех аккаунтов.
    По ТЗ: информационные панели со статусами и датами.
    """
    if not accounts:
        ui.warn("Нет аккаунтов для анализа")
        return

    total = len(accounts)
    now = datetime.now()

    # ── Статусы ──────────────────────────────────────────────
    by_status = {}
    for a in accounts:
        s = a.get("status", "unknown")
        by_status[s] = by_status.get(s, 0) + 1

    active    = by_status.get("active", 0)
    spamblock = by_status.get("spamblock", 0)
    frozen    = by_status.get("frozen", 0)
    quarantine= by_status.get("quarantine", 0)
    error     = by_status.get("error", 0)
    unknown   = by_status.get("unknown", 0)

    # ── Trust Score ───────────────────────────────────────────
    scores = [a.get("trust_score", 0) for a in accounts]
    avg_trust = sum(scores) // len(scores) if scores else 0
    max_trust = max(scores) if scores else 0
    min_trust = min(scores) if scores else 0

    score_buckets = {
        "Отличный (80-100)": sum(1 for s in scores if s >= 80),
        "Хороший (60-79)":   sum(1 for s in scores if 60 <= s < 80),
        "Средний (40-59)":   sum(1 for s in scores if 40 <= s < 60),
        "Слабый (20-39)":    sum(1 for s in scores if 20 <= s < 40),
        "Критический (0-19)":sum(1 for s in scores if s < 20),
    }

    # ── Проверки ─────────────────────────────────────────────
    checked_today = 0
    checked_week  = 0
    never_checked = 0

    for a in accounts:
        lc = a.get("last_checked")
        if not lc:
            never_checked += 1
            continue
        try:
            dt = datetime.fromisoformat(lc)
            diff = (now - dt).days
            if diff == 0:
                checked_today += 1
            if diff <= 7:
                checked_week += 1
        except Exception:
            never_checked += 1

    # ── Профили ──────────────────────────────────────────────
    with_username = sum(1 for a in accounts if a.get("username"))
    with_bio      = sum(1 for a in accounts if a.get("bio"))
    with_photo    = sum(1 for a in accounts if a.get("has_photo"))
    with_proxy    = sum(1 for a in accounts if a.get("proxy"))
    with_2fa      = sum(1 for a in accounts if a.get("has_2fa"))

    # ── Вывод ────────────────────────────────────────────────
    print(f"""
\033[36m{'═' * 56}\033[0m
\033[36m  GramGPT — Health Dashboard\033[0m
\033[36m{'═' * 56}\033[0m

  \033[37mВсего аккаунтов: \033[0m\033[97m{total}\033[0m

\033[36m  ── Статусы ─────────────────────────────────────────\033[0m
  \033[32m✅ Активных:    {active:<4}\033[0m  ({_pct(active, total)}%)
  \033[31m🚫 Спамблок:    {spamblock:<4}\033[0m  ({_pct(spamblock, total)}%)
  \033[33m❄️  Заморожено:  {frozen:<4}\033[0m  ({_pct(frozen, total)}%)
  \033[35m⛔ Карантин:    {quarantine:<4}\033[0m  ({_pct(quarantine, total)}%)
  \033[31m❌ Ошибка:      {error:<4}\033[0m  ({_pct(error, total)}%)
  \033[37m❓ Не проверено:{unknown:<4}\033[0m  ({_pct(unknown, total)}%)

\033[36m  ── Trust Score ─────────────────────────────────────\033[0m
  Средний:  {ui.trust_bar(avg_trust)}
  Макс:     {max_trust}/100   Мин: {min_trust}/100
""")

    for grade, count in score_buckets.items():
        if count > 0:
            bar = "▓" * count + "░" * (total - count)
            print(f"  {grade:<22} {count:>3}  [{bar[:20]}]")

    print(f"""
\033[36m  ── Проверки ────────────────────────────────────────\033[0m
  Проверено сегодня:  {checked_today}
  Проверено за неделю:{checked_week}
  Никогда не проверено:{never_checked}

\033[36m  ── Заполненность профилей ──────────────────────────\033[0m
  Username:  {with_username}/{total}  {'█' * with_username + '░' * (total - with_username)}
  Bio:       {with_bio}/{total}  {'█' * with_bio + '░' * (total - with_bio)}
  Фото:      {with_photo}/{total}  {'█' * with_photo + '░' * (total - with_photo)}
  Прокси:    {with_proxy}/{total}  {'█' * with_proxy + '░' * (total - with_proxy)}
  2FA:       {with_2fa}/{total}  {'█' * with_2fa + '░' * (total - with_2fa)}

\033[36m{'═' * 56}\033[0m""")


# ============================================================
# ДЕТАЛЬНЫЙ ПРОСМОТР АККАУНТА С РЕКОМЕНДАЦИЯМИ
# По ТЗ: детальный просмотр + подсказки по Trust Score
# ============================================================

def account_detail(account: dict):
    """Подробная карточка аккаунта с рекомендациями"""
    phone = account.get("phone", "?")
    status = account.get("status", "unknown")
    score = account.get("trust_score", 0)
    added = (account.get("added_at") or "?")[:10]
    checked = (account.get("last_checked") or "никогда")[:16].replace("T", " ")

    # Возраст аккаунта в базе
    try:
        added_dt = datetime.fromisoformat(account.get("added_at", ""))
        days_in_db = (datetime.now() - added_dt).days
        age_str = f"{days_in_db} дней в базе"
    except Exception:
        age_str = "дата неизвестна"

    # Количество активных сессий
    sessions = account.get("active_sessions", "?")

    print(f"""
\033[36m{'─' * 56}\033[0m
  \033[97m{account.get('first_name','')} {account.get('last_name','')}\033[0m  @{account.get('username','—')}
  📱 {phone}
\033[36m{'─' * 56}\033[0m
  Статус:        {ui.status_icon(status)}
  Trust Score:   {ui.trust_bar(score)}
  Активных сессий: {sessions}
  В базе:        {age_str}
  Добавлен:      {added}
  Проверен:      {checked}
  Роль:          {account.get('role', 'default')}
  Теги:          {', '.join(account.get('tags', []) or ['—'])}
  Прокси:        {account.get('proxy') or '—'}
  2FA:           {'✅' if account.get('has_2fa') else '❌'}
  Каналов:       {len(account.get('channels', []))}
  Bio:           {(account.get('bio') or '—')[:60]}
  Заметка:       {(account.get('notes') or '—')[:60]}
\033[36m{'─' * 56}\033[0m""")

    # Рекомендации
    tips = trust_module.get_recommendations(account)
    if sessions != "?" and isinstance(sessions, int) and sessions > 5:
        tips.append(f"⚠️  {sessions} активных сессий — завершить лишние (раздел Безопасность)")
    if not account.get("proxy"):
        tips.append("🔒 Назначь прокси (раздел Прокси)")
    if account.get("last_checked") is None:
        tips.append("🔍 Аккаунт ни разу не проверялся — запусти проверку (пункт 3)")

    if tips:
        print(f"  \033[33m💡 Рекомендации:\033[0m")
        for tip in tips:
            print(f"  \033[33m   • {tip}\033[0m")
        print()


# ============================================================
# ПОИСК И ФИЛЬТРАЦИЯ
# По ТЗ: поиск по номеру, username, статусу, тегам
# ============================================================

def search_accounts(accounts: list[dict], query: str) -> list[dict]:
    """
    Ищет аккаунты по запросу.
    Поддерживает: номер телефона, username, имя, тег, роль, статус
    """
    q = query.lower().strip()
    if not q:
        return accounts

    results = []
    for a in accounts:
        if (
            q in (a.get("phone") or "").lower() or
            q in (a.get("username") or "").lower() or
            q in (a.get("first_name") or "").lower() or
            q in (a.get("last_name") or "").lower() or
            q in (a.get("status") or "").lower() or
            q in (a.get("role") or "").lower() or
            q in (a.get("notes") or "").lower() or
            any(q in tag.lower() for tag in a.get("tags", []))
        ):
            results.append(a)

    return results


def filter_accounts(accounts: list[dict],
                    status: str = None,
                    role: str = None,
                    min_trust: int = None,
                    max_trust: int = None,
                    has_proxy: bool = None,
                    has_username: bool = None,
                    tag: str = None) -> list[dict]:
    """Фильтрует аккаунты по параметрам"""
    result = accounts

    if status:
        result = [a for a in result if a.get("status") == status]
    if role:
        result = [a for a in result if a.get("role") == role]
    if min_trust is not None:
        result = [a for a in result if a.get("trust_score", 0) >= min_trust]
    if max_trust is not None:
        result = [a for a in result if a.get("trust_score", 0) <= max_trust]
    if has_proxy is not None:
        result = [a for a in result
                  if bool(a.get("proxy")) == has_proxy]
    if has_username is not None:
        result = [a for a in result
                  if bool(a.get("username")) == has_username]
    if tag:
        result = [a for a in result if tag in (a.get("tags") or [])]

    return result


def sort_accounts(accounts: list[dict], by: str = "trust",
                  reverse: bool = True) -> list[dict]:
    """
    Сортирует аккаунты.
    by: trust | status | added | checked | phone
    """
    key_map = {
        "trust":   lambda a: a.get("trust_score", 0),
        "status":  lambda a: a.get("status", ""),
        "added":   lambda a: a.get("added_at", ""),
        "checked": lambda a: a.get("last_checked", ""),
        "phone":   lambda a: a.get("phone", ""),
    }
    key = key_map.get(by, key_map["trust"])
    return sorted(accounts, key=key, reverse=reverse)


# ============================================================
# ВСПОМОГАТЕЛЬНАЯ
# ============================================================

def _pct(part: int, total: int) -> str:
    if total == 0:
        return "0"
    return str(round(part / total * 100))
