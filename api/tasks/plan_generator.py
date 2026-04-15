"""
GramGPT — tasks/plan_generator.py
Генератор дневных планов для аккаунтов.

Чистая логика — без БД, без Celery, без импортов моделей.
Принимает данные, возвращает dict с планом.

Максимальная рандомизация:
  - Время сессий: не фиксированные окна, а произвольные
  - Количество действий: ±50% от базового
  - Порядок действий: рандомный
  - Шанс пропуска сессии: 8-15%
  - "Ленивые дни": 20% шанс сделать вполовину меньше
  - "Активные дни": 15% шанс сделать в 1.5 раза больше
  - Паузы между действиями: от 3с до 120с
  - Иногда "отлучился" посреди сессии (30-180с пауза)
"""

import random
import hashlib
from datetime import datetime, timedelta


# ═══════════════════════════════════════════════════════════
# РАСПРЕДЕЛЕНИЕ КОММЕНТАРИЕВ
# ═══════════════════════════════════════════════════════════

def distribute_comments_by_days(total_comments, total_days):
    """
    Распределяет комменты по дням кампании. НЕ равномерно.
    Первые дни меньше (прогрев), потом нарастает, к концу снижается.
    """
    if total_days <= 0:
        return []

    avg = max(1, total_comments / total_days)
    daily = []
    remaining = total_comments

    for day in range(1, total_days + 1):
        if day <= 2:
            # Прогрев: 0-25% от среднего
            today = int(avg * random.uniform(0, 0.25))
        elif day <= 4:
            # Разогрев: 40-75% от среднего
            today = int(avg * random.uniform(0.4, 0.75))
        elif day >= total_days - 1:
            # Последние 2 дня: снижение 50-90%
            today = int(avg * random.uniform(0.5, 0.9))
        else:
            # Полная мощность: 70-150% от среднего (широкий разброс!)
            today = int(avg * random.uniform(0.7, 1.5))

        today = max(0, min(today, remaining))
        daily.append(today)
        remaining -= today

    # Остаток раскидываем по дням 3+
    while remaining > 0:
        candidates = [i for i in range(min(2, total_days), total_days) if daily[i] < total_comments // 2]
        if not candidates:
            candidates = list(range(total_days))
        idx = random.choice(candidates)
        daily[idx] += 1
        remaining -= 1

    return daily


def distribute_day_comments(comments_today, accounts_with_personalities):
    """
    Распределяет дневную квоту по аккаунтам.
    Возвращает {account_id: количество_комментов}
    """
    distribution = {}
    remaining = comments_today

    shuffled = list(accounts_with_personalities)
    random.shuffle(shuffled)

    for acc_id, personality in shuffled:
        if remaining <= 0:
            distribution[acc_id] = 0
            continue

        # Шанс что этот аккаунт сегодня комментирует
        if random.random() > personality.get("comment_chance", 0.3):
            distribution[acc_id] = 0
            continue

        name = personality.get("name", "active_reader")
        if name == "lurker":
            count = random.choices([0, 1], weights=[55, 45])[0]
        elif name == "reactor":
            count = random.choices([0, 1], weights=[65, 35])[0]
        elif name == "active_reader":
            count = random.choices([0, 1, 2], weights=[20, 55, 25])[0]
        elif name == "commenter":
            count = random.choices([1, 2, 3], weights=[25, 50, 25])[0]
        elif name == "night_owl":
            count = random.choices([0, 1, 2], weights=[25, 50, 25])[0]
        else:
            count = random.randint(0, 2)

        count = min(count, 3, remaining)
        distribution[acc_id] = count
        remaining -= count

    # Остаток — раздаём случайным
    while remaining > 0:
        candidates = [a for a, c in distribution.items() if c < 3]
        if not candidates:
            break
        acc_id = random.choice(candidates)
        distribution[acc_id] += 1
        remaining -= 1

    return distribution


# ═══════════════════════════════════════════════════════════
# ГЕНЕРАТОР ПЛАНА ДНЯ
# ═══════════════════════════════════════════════════════════

# Пул сообщений для saved messages
SAVED_MESSAGES = [
    "ок", "👍", "✅", "📌", "🔖", "!", "потом", "важно",
    "запомнить", "перевірити", "зробити", "📎", "💡", "🔴",
    "check", "todo", "read later", "⭐", "!!!", "...",
]

# Пул эмодзи для реакций (больше вариантов)
REACTION_EMOJIS = ["👍", "🔥", "❤️", "🤔", "👏", "😂", "🎉", "😮", "🥰", "💯", "⚡", "🙏"]


def _pick_day_mood():
    """
    Рандомное 'настроение' дня.
    Влияет на количество сессий и действий.
    """
    moods = [
        {"name": "lazy", "session_mult": 0.7, "action_mult": 0.6, "weight": 15},
        {"name": "tired", "session_mult": 0.8, "action_mult": 0.7, "weight": 15},
        {"name": "normal", "session_mult": 1.0, "action_mult": 1.0, "weight": 35},
        {"name": "active", "session_mult": 1.2, "action_mult": 1.3, "weight": 20},
        {"name": "hyper", "session_mult": 1.5, "action_mult": 1.6, "weight": 10},
        {"name": "focused", "session_mult": 0.8, "action_mult": 1.4, "weight": 5},  # мало сессий но долгие
    ]
    weights = [m["weight"] for m in moods]
    return random.choices(moods, weights=weights, k=1)[0]


def generate_daily_plan(
    account_id, phone, campaign_channels, campaign_id,
    day_number, comments_today=0, personality=None,
    timing=None, style=None
):
    """
    Генерирует УНИКАЛЬНЫЙ план дня для одного аккаунта.
    Каждый вызов = абсолютно другой план.
    """

    if not personality:
        personality = {"name": "active_reader", "comment_chance": 0.3,
                       "session_count_min": 2, "session_count_max": 5}
    if not timing:
        timing = {"name": "normal"}
    if not style:
        style = {"name": "thinker"}

    p_name = personality.get("name", "active_reader")

    # ── Настроение дня ───────────────────────────────────
    mood = _pick_day_mood()

    # ── Сколько сессий ───────────────────────────────────
    base_sessions = random.randint(
        personality.get("session_count_min", 2),
        personality.get("session_count_max", 5),
    )

    # День 1-3: меньше сессий (прогрев)
    if day_number <= 1:
        base_sessions = max(1, base_sessions - random.randint(2, 3))
    elif day_number <= 3:
        base_sessions = max(1, base_sessions - random.randint(1, 2))

    # Применяем настроение
    num_sessions = max(1, int(base_sessions * mood["session_mult"]))

    # Ещё раз рандом: ±1
    num_sessions = max(1, num_sessions + random.choice([-1, 0, 0, 0, 0, 1]))

    # Гарантируем достаточно сессий для комментариев
    if comments_today > 0:
        num_sessions = max(num_sessions, comments_today + random.randint(0, 2))

    # ── Времена сессий ───────────────────────────────────
    # Не фиксированные окна! Рандомно по всему дню
    session_times = []
    # Если это первый день (сегодня) — не планируем раньше чем сейчас + 10 мин
    from datetime import datetime as dt
    now = dt.utcnow()
    current_hour = (now.hour + 3) % 24  # UTC+3
    current_minute = now.minute
    min_start_hour = current_hour if day_number == 1 else 8
    min_start_minute = (current_minute + 10) if day_number == 1 else 0

    if p_name == "night_owl":
        for _ in range(num_sessions):
            if random.random() < 0.7:
                lo, hi = max(min_start_hour, 17), 23
            else:
                lo, hi = max(min_start_hour, 10), 16
            if lo > hi:
                lo, hi = min_start_hour, 23
            session_times.append((random.randint(lo, hi), random.randint(0, 59)))
    elif p_name == "lurker":
        for _ in range(num_sessions):
            if random.random() < 0.5:
                lo, hi = max(min_start_hour, 19), 23
            else:
                lo, hi = max(min_start_hour, 8), 18
            if lo > hi:
                lo, hi = min_start_hour, 23
            session_times.append((random.randint(lo, hi), random.randint(0, 59)))
    else:
        for _ in range(num_sessions):
            if random.random() < 0.4:
                lo, hi = max(min_start_hour, 8), 13
            elif random.random() < 0.6:
                lo, hi = max(min_start_hour, 13), 18
            else:
                lo, hi = max(min_start_hour, 18), 23
            if lo > hi:
                lo, hi = min_start_hour, 23
            session_times.append((random.randint(lo, hi), random.randint(0, 59)))

    session_times.sort()

    # Минимум 20-90 минут между сессиями (рандомно!)
    min_gap = random.randint(20, 90)
    for i in range(1, len(session_times)):
        prev_min = session_times[i-1][0] * 60 + session_times[i-1][1]
        curr_min = session_times[i][0] * 60 + session_times[i][1]
        if curr_min - prev_min < min_gap:
            new_min = prev_min + min_gap + random.randint(0, 30)
            new_h = min(23, new_min // 60)
            new_m = new_min % 60
            session_times[i] = (new_h, new_m)

    # Убираем сессии раньше текущего времени (для первого дня)
    if day_number == 1:
        session_times = [(h, m) for h, m in session_times
                         if h > current_hour or (h == current_hour and m >= min_start_minute)]
        if not session_times:
            next_h = current_hour
            next_m = current_minute + 15
            if next_m >= 60:
                next_h += 1
                next_m -= 60
            if next_h <= 23:
                session_times = [(next_h, next_m)]

    # ── Распределяем комментарии по сессиям ──────────────
    comment_sessions = set()
    if comments_today > 0:
        available = list(range(num_sessions))
        # Не ставим коммент в первую сессию (если >2 сессий)
        if len(available) > 2:
            available = available[1:]
        random.shuffle(available)
        for i in range(min(comments_today, len(available))):
            comment_sessions.add(available[i])

    # Каналы для комментариев
    comment_channels = []
    if comments_today > 0 and campaign_channels:
        comment_channels = random.sample(
            campaign_channels,
            min(comments_today, len(campaign_channels))
        )

    # ── Генерируем действия ──────────────────────────────
    sessions = []
    comment_idx = 0

    for sess_i, (hour, minute) in enumerate(session_times):
        has_comment = sess_i in comment_sessions and comment_idx < len(comment_channels)
        actions = []

        # Количество действий
        if has_comment:
            base_actions = random.randint(4, 12)
        else:
            base_actions = random.randint(2, 8)

        # День 1-3: чуть меньше, но не меньше 3
        if day_number <= 1:
            base_actions = max(3, int(base_actions * 0.5))
        elif day_number <= 3:
            base_actions = max(3, int(base_actions * 0.7))

        # Настроение
        num_actions = max(1, int(base_actions * mood["action_mult"]))

        # Ещё ±1-2
        num_actions = max(3, num_actions + random.choice([-1, 0, 0, 0, 0, 1, 1, 2]))

        # Доступные действия (зависят от дня)
        if day_number <= 2:
            available_actions = [
                ("read_feed", 50), ("view_stories", 30),
                ("set_reaction", 10), ("view_profile", 10),
            ]
        elif day_number <= 4:
            available_actions = [
                ("read_feed", 35), ("view_stories", 18),
                ("set_reaction", 15), ("view_profile", 8),
                ("search", 5), ("send_saved", 10),
                ("forward_saved", 5), ("reply_dm", 4),
            ]
        else:
            available_actions = [
                ("read_feed", 28), ("view_stories", 12),
                ("set_reaction", 15), ("view_profile", 6),
                ("search", 5), ("forward_saved", 7),
                ("send_saved", 10), ("reply_dm", 7),
                ("join_channel", 3), ("typing", 4),
            ]

        # Позиция комментария
        if has_comment:
            comment_pos = random.randint(
                max(1, num_actions // 4),
                max(2, num_actions * 3 // 4)
            )
        else:
            comment_pos = -1

        action_i = 0
        while action_i < num_actions:
            # ── Вставка комментария ──────────────────────
            if action_i == comment_pos and has_comment and comment_idx < len(comment_channels):
                target = comment_channels[comment_idx]
                comment_idx += 1

                # Pre-read целевого канала
                actions.append({
                    "type": "read_feed",
                    "channel": target,
                    "count": random.randint(2, 10),
                    "pause_after": random.randint(10, 180),  # Широкий диапазон!
                })

                # Реакция (рандомный шанс 10-55%)
                if random.random() < random.uniform(0.10, 0.55):
                    actions.append({
                        "type": "set_reaction",
                        "channel": target,
                        "emoji": random.choice(REACTION_EMOJIS),
                        "pause_after": random.randint(3, 40),
                    })

                # Комментарий
                actions.append({
                    "type": "smart_comment",
                    "channel": target,
                    "pause_before": random.randint(2, 30),  # typing
                })

                # Post-read (не уходить сразу)
                actions.append({
                    "type": "read_feed",
                    "channel": None,
                    "count": random.randint(1, 5),
                    "pause_after": random.randint(3, 30),
                })

                action_i += 1
                continue

            # ── Обычное действие ─────────────────────────
            weights = [w for _, w in available_actions]
            names = [n for n, _ in available_actions]
            chosen = random.choices(names, weights=weights, k=1)[0]

            action = {"type": chosen}

            if chosen == "read_feed":
                action["count"] = random.randint(1, 15)
                action["pause_after"] = random.randint(3, 90)
                # Иногда читаем конкретный канал из подписок
                if campaign_channels and random.random() < 0.3:
                    action["channel"] = random.choice(campaign_channels)
            elif chosen == "view_stories":
                action["count"] = random.randint(1, 10)
            elif chosen == "set_reaction":
                action["emoji"] = random.choice(REACTION_EMOJIS)
                action["pause_after"] = random.randint(2, 25)
            elif chosen == "send_saved":
                action["text"] = random.choice(SAVED_MESSAGES)
            elif chosen == "view_profile":
                action["pause_after"] = random.randint(3, 20)
            elif chosen == "search":
                action["pause_after"] = random.randint(5, 30)
            elif chosen == "typing":
                action["duration"] = random.randint(2, 15)

            # Рандомная пауза после действия (если не задана)
            if "pause_after" not in action:
                action["pause_after"] = random.randint(3, 45)

            actions.append(action)

            # 5% шанс "отлучился" — длинная пауза посреди сессии
            if random.random() < 0.05 and action_i < num_actions - 1:
                actions.append({
                    "type": "idle",
                    "duration": random.randint(30, 180),
                    "detail": random.choice([
                        "отвлёкся", "ушёл", "пауза", "перерыв"
                    ]),
                })

            action_i += 1

        # 8% шанс ПРОПУСТИТЬ сессию целиком (как реальный человек)
        if random.random() < 0.08 and not has_comment:
            sessions.append({
                "connect_at_hour": hour,
                "connect_at_minute": minute,
                "actions": [],
                "skipped": True,
                "skip_reason": random.choice([
                    "забыл", "занят", "не до телефона", "сон"
                ]),
            })
            continue

        # Джиттер времени: ±0-20 минут
        jitter = random.randint(-15, 20)
        adjusted_min = hour * 60 + minute + jitter
        adjusted_min = max(8 * 60, min(23 * 60 + 50, adjusted_min))

        sessions.append({
            "connect_at_hour": adjusted_min // 60,
            "connect_at_minute": adjusted_min % 60,
            "actions": actions,
            "skipped": False,
        })

        join_found = 0
        for s in sessions:
            if s.get("skipped"):
                continue
            filtered_actions = []
            for a in s.get("actions", []):
                if a.get("type") == "join_channel":
                    join_found += 1
                    if join_found > 7:
                        continue  # Пропускаем лишние
                filtered_actions.append(a)
            s["actions"] = filtered_actions

    # Считаем реально размещённые комменты (не запрошенные)
    actual_comments = sum(
        1 for s in sessions
        for a in s.get("actions", [])
        if a.get("type") == "smart_comment"
    )

    return {
        "account_id": account_id,
        "campaign_id": campaign_id,
        "personality": personality.get("name", "unknown"),
        "timing": timing.get("name", "unknown"),
        "style": style.get("name", "unknown"),
        "mood": mood["name"],
        "day_number": day_number,
        "total_comments": actual_comments,
        "total_sessions": len(sessions),
        "sessions": sessions,
    }
