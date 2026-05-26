"""
GramGPT — tasks/plan_generator.py
Генератор дневных планов для аккаунтов.

Чистая логика — без БД, без Celery, без импортов моделей.
Принимает данные, возвращает dict с планом.

Органическая воронка подписок:
  - channels_to_join        — каналы, на которые СЕГОДНЯ нужно подписаться (1–2 макс.)
  - commentable_from_prev_days — каналы, на которые подписались в предыдущих днях/сессиях.
  - Внутри дня: после вступления в канал (session i) он становится commentable в session i+1.
  - Жёсткое правило: smart_comment ТОЛЬКО в commentable каналах.
"""

import random
from datetime import datetime


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
            today = int(avg * random.uniform(0, 0.25))
        elif day <= 4:
            today = int(avg * random.uniform(0.4, 0.75))
        elif day >= total_days - 1:
            today = int(avg * random.uniform(0.5, 0.9))
        else:
            today = int(avg * random.uniform(0.7, 1.5))

        today = max(0, min(today, remaining))
        daily.append(today)
        remaining -= today

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

        if random.random() > personality.get("comment_chance", 0.3):
            distribution[acc_id] = 0
            continue

        name = personality.get("name", "active_reader")
        if name == "lurker":
            count = random.choices([0, 1, 2], weights=[50, 40, 10])[0]
        elif name == "reactor":
            count = random.choices([0, 1, 2], weights=[60, 30, 10])[0]
        elif name == "active_reader":
            count = random.choices([0, 1, 2, 3], weights=[15, 45, 25, 15])[0]
        elif name == "commenter":
            count = random.choices([1, 2, 3, 4, 5], weights=[15, 30, 30, 15, 10])[0]
        elif name == "night_owl":
            count = random.choices([0, 1, 2, 3], weights=[20, 40, 25, 15])[0]
        else:
            count = random.randint(0, 3)

        count = min(count, 6, remaining)
        distribution[acc_id] = count
        remaining -= count

    while remaining > 0:
        candidates = [a for a, c in distribution.items() if c < 6]
        if not candidates:
            break
        acc_id = random.choice(candidates)
        distribution[acc_id] += 1
        remaining -= 1

    return distribution


# ═══════════════════════════════════════════════════════════
# ГЕНЕРАТОР ПЛАНА ДНЯ
# ═══════════════════════════════════════════════════════════

SAVED_MESSAGES = [
    "ок", "👍", "✅", "📌", "🔖", "!", "потом", "важно",
    "запомнить", "перевірити", "зробити", "📎", "💡", "🔴",
    "check", "todo", "read later", "⭐", "!!!", "...",
]

REACTION_EMOJIS = ["👍", "🔥", "❤️", "🤔", "👏", "😂", "🎉", "😮", "🥰", "💯", "⚡", "🙏"]


def _pick_day_mood():
    moods = [
        {"name": "lazy",    "session_mult": 0.7, "action_mult": 0.6, "weight": 15},
        {"name": "tired",   "session_mult": 0.8, "action_mult": 0.7, "weight": 15},
        {"name": "normal",  "session_mult": 1.0, "action_mult": 1.0, "weight": 35},
        {"name": "active",  "session_mult": 1.2, "action_mult": 1.3, "weight": 20},
        {"name": "hyper",   "session_mult": 1.5, "action_mult": 1.6, "weight": 10},
        {"name": "focused", "session_mult": 0.8, "action_mult": 1.4, "weight": 5},
    ]
    weights = [m["weight"] for m in moods]
    return random.choices(moods, weights=weights, k=1)[0]


def generate_daily_plan(
    account_id, phone, campaign_channels, campaign_id,
    day_number, comments_today=0, personality=None,
    timing=None, style=None,
    channels_to_join=None,            # каналы для вступления СЕГОДНЯ (1–2)
    commentable_from_prev_days=None,  # каналы, в которых уже можно комментировать
):
    """
    Генерирует УНИКАЛЬНЫЙ план дня для одного аккаунта.

    Органическая воронка:
    - В session[i] аккаунт вступает в channels_to_join[i] (если есть).
    - Commentable для session[i] = commentable_from_prev_days + каналы, вступление
      в которые произошло в session[0..i-1] ЭТОГО же дня.
    - smart_comment ставится ТОЛЬКО в commentable каналах.
    """

    if not personality:
        personality = {"name": "active_reader", "comment_chance": 0.3,
                       "session_count_min": 2, "session_count_max": 5}
    if not timing:
        timing = {"name": "normal"}
    if not style:
        style = {"name": "thinker"}

    channels_to_join = list(channels_to_join or [])
    commentable_start = list(commentable_from_prev_days or [])

    p_name = personality.get("name", "active_reader")
    mood = _pick_day_mood()

    # ── Количество сессий ────────────────────────────────
    base_sessions = random.randint(
        personality.get("session_count_min", 2),
        personality.get("session_count_max", 5),
    )
    if day_number <= 1:
        base_sessions = max(1, base_sessions - random.randint(2, 3))
    elif day_number <= 3:
        base_sessions = max(1, base_sessions - random.randint(1, 2))

    num_sessions = max(1, int(base_sessions * mood["session_mult"]))
    num_sessions = max(1, num_sessions + random.choice([-1, 0, 0, 0, 0, 1]))

    # Гарантируем достаточно сессий для запланированных вступлений и комментов
    min_needed = max(len(channels_to_join), (comments_today // 2) + 1 if comments_today > 0 else 1)
    num_sessions = max(num_sessions, min_needed)

    # ── Времена сессий ───────────────────────────────────
    now = datetime.utcnow()
    current_hour   = (now.hour + 3) % 24   # UTC+3
    current_minute = now.minute
    min_start_hour   = current_hour   if day_number == 1 else 8
    min_start_minute = (current_minute + 10) if day_number == 1 else 0

    session_times = []
    if p_name == "night_owl":
        for _ in range(num_sessions):
            lo = max(min_start_hour, 17) if random.random() < 0.7 else max(min_start_hour, 10)
            hi = 23 if lo >= 17 else 16
            if lo > hi:
                lo, hi = min_start_hour, 23
            session_times.append((random.randint(lo, hi), random.randint(0, 59)))
    elif p_name == "lurker":
        for _ in range(num_sessions):
            lo = max(min_start_hour, 19) if random.random() < 0.5 else max(min_start_hour, 8)
            hi = 23 if lo >= 19 else 18
            if lo > hi:
                lo, hi = min_start_hour, 23
            session_times.append((random.randint(lo, hi), random.randint(0, 59)))
    else:
        for _ in range(num_sessions):
            r = random.random()
            if r < 0.4:
                lo, hi = max(min_start_hour, 8), 13
            elif r < 0.7:
                lo, hi = max(min_start_hour, 13), 18
            else:
                lo, hi = max(min_start_hour, 18), 23
            if lo > hi:
                lo, hi = min_start_hour, 23
            session_times.append((random.randint(lo, hi), random.randint(0, 59)))

    session_times.sort()

    min_gap = random.randint(20, 90)
    for i in range(1, len(session_times)):
        prev_min = session_times[i-1][0] * 60 + session_times[i-1][1]
        curr_min = session_times[i][0] * 60 + session_times[i][1]
        if curr_min - prev_min < min_gap:
            new_min = prev_min + min_gap + random.randint(0, 30)
            new_h = min(23, new_min // 60)
            new_m = new_min % 60
            session_times[i] = (new_h, new_m)

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

    # ═══════════════════════════════════════════════════
    # ЦИКЛ СЕССИЙ — органическая воронка подписок
    # ═══════════════════════════════════════════════════

    joined_today   = []         # каналы, вступление в которые произошло СЕГОДНЯ
    remaining_comments = comments_today
    sessions = []

    for sess_i, (hour, minute) in enumerate(session_times):

        # Что commentable СЕЙЧАС (до действий этой сессии)
        sess_commentable = commentable_start + joined_today

        # Вступаем ли в канал в этой сессии?
        join_this_sess = channels_to_join[sess_i] if sess_i < len(channels_to_join) else None
        if join_this_sess:
            joined_today.append(join_this_sess)

        # ── Сколько комментов в этой сессии ─────────────
        sessions_left = len(session_times) - sess_i
        sess_c_count = 0
        # Нельзя комментировать в первой сессии (правило: предыдущая сессия)
        # И только если есть commentable каналы
        if remaining_comments > 0 and sess_commentable and sess_i >= 1:
            avg = max(1, remaining_comments / sessions_left)
            sess_c_count = random.choices(
                [0, max(1, round(avg)), max(1, round(avg)) + 1],
                weights=[25, 50, 25], k=1
            )[0]
            sess_c_count = min(sess_c_count, 3, remaining_comments)

        remaining_comments = max(0, remaining_comments - sess_c_count)

        # ── Цели комментариев: ТОЛЬКО commentable ────────
        sess_c_targets = []
        if sess_c_count > 0 and sess_commentable:
            sess_c_targets = random.sample(
                sess_commentable,
                min(sess_c_count, len(sess_commentable)),
            )

        # ── Реакции: тоже только в commentable ──────────
        sess_r_targets = []
        if sess_commentable and random.random() < 0.5:
            n_r = random.randint(1, min(3, len(sess_commentable)))
            sess_r_targets = random.sample(sess_commentable, n_r)

        has_targeted = bool(join_this_sess) or bool(sess_c_targets) or bool(sess_r_targets)

        # ── Количество действий в сессии ────────────────
        if has_targeted:
            base_actions = random.randint(4, 12) + (len(sess_c_targets) + len(sess_r_targets)) * 2
        else:
            base_actions = random.randint(2, 8)

        if day_number <= 1:
            base_actions = max(3, int(base_actions * 0.5))
        elif day_number <= 3:
            base_actions = max(3, int(base_actions * 0.7))

        num_actions = max(1, int(base_actions * mood["action_mult"]))
        num_actions = max(3, num_actions + random.choice([-1, 0, 0, 0, 0, 1, 1, 2]))
        num_actions = max(num_actions, len(sess_c_targets) + len(sess_r_targets) + (1 if join_this_sess else 0) + 2)

        # ── Доступные фоновые действия ───────────────────
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
                ("typing", 4),
            ]

        # ── Строим список действий ───────────────────────
        actions = []

        # 1. Вступление в канал — ПЕРВЫМ действием сессии
        if join_this_sess:
            actions.append({
                "type": "join_target_channel",
                "channel": join_this_sess,
                "pause_after": random.randint(20, 60),
            })
            # Органично: почитать немного после вступления
            actions.append({
                "type": "read_feed",
                "channel": join_this_sess,
                "count": random.randint(3, 8),
                "pause_after": random.randint(15, 60),
            })

        # 2. Остальные действия (фон + целевые)
        c_targets_left = list(sess_c_targets)
        r_targets_left = list(sess_r_targets)

        # Сколько "свободных" слотов после join + обязательных целевых
        mandatory_slots = (2 if join_this_sess else 0) + len(c_targets_left) * 3 + len(r_targets_left) * 2
        free_slots = max(2, num_actions - mandatory_slots)

        action_i = 0
        while action_i < free_slots:
            remaining_targets = len(c_targets_left) + len(r_targets_left)
            remaining_slots   = free_slots - action_i

            # Вставляем целевой коммент или реакцию
            if remaining_targets > 0 and (random.random() < 0.35 or remaining_targets >= remaining_slots):
                is_comment = False
                if c_targets_left and r_targets_left:
                    is_comment = random.random() < 0.5
                elif c_targets_left:
                    is_comment = True

                if is_comment:
                    target = c_targets_left.pop(0)
                    actions.append({
                        "type": "read_feed",
                        "channel": target,
                        "count": random.randint(2, 10),
                        "pause_after": random.randint(10, 180),
                    })
                    if random.random() < random.uniform(0.10, 0.55):
                        actions.append({
                            "type": "set_reaction",
                            "channel": target,
                            "emoji": random.choice(REACTION_EMOJIS),
                            "pause_after": random.randint(3, 40),
                        })
                    actions.append({
                        "type": "smart_comment",
                        "channel": target,
                        "pause_before": random.randint(2, 30),
                    })
                    # С некоторой вероятностью — ставим реакцию(-и) на комменты в обсуждении
                    _react_roll = random.random()
                    if _react_roll < 0.15:
                        actions.append({
                            "type": "react_to_comment",
                            "channel": target,
                            "count": 2,
                            "pause_after": random.randint(3, 12),
                        })
                    elif _react_roll < 0.40:
                        actions.append({
                            "type": "react_to_comment",
                            "channel": target,
                            "count": 1,
                            "pause_after": random.randint(3, 12),
                        })
                    actions.append({
                        "type": "read_feed",
                        "channel": None,
                        "count": random.randint(1, 5),
                        "pause_after": random.randint(3, 30),
                    })
                else:
                    target = r_targets_left.pop(0)
                    actions.append({
                        "type": "read_feed",
                        "channel": target,
                        "count": random.randint(2, 6),
                        "pause_after": random.randint(5, 30),
                    })
                    actions.append({
                        "type": "set_reaction",
                        "channel": target,
                        "emoji": random.choice(REACTION_EMOJIS),
                        "pause_after": random.randint(5, 20),
                    })

                action_i += 1
                continue

            # Фоновое действие
            weights = [w for _, w in available_actions]
            names   = [n for n, _ in available_actions]
            chosen  = random.choices(names, weights=weights, k=1)[0]
            action  = {"type": chosen}

            if chosen == "read_feed":
                action["count"] = random.randint(1, 15)
                action["pause_after"] = random.randint(3, 90)
                # Иногда читаем commentable канал
                if sess_commentable and random.random() < 0.3:
                    action["channel"] = random.choice(sess_commentable)
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

            if "pause_after" not in action:
                action["pause_after"] = random.randint(3, 45)

            actions.append(action)

            if random.random() < 0.05 and action_i < free_slots - 1:
                actions.append({
                    "type": "idle",
                    "duration": random.randint(30, 180),
                    "detail": random.choice(["отвлёкся", "ушёл", "пауза", "перерыв"]),
                })

            action_i += 1

        # 8% шанс пропустить сессию (только без целевых действий)
        if random.random() < 0.08 and not has_targeted:
            sessions.append({
                "connect_at_hour":   hour,
                "connect_at_minute": minute,
                "actions": [],
                "skipped": True,
                "skip_reason": random.choice(["забыл", "занят", "не до телефона", "сон"]),
            })
            continue

        jitter = random.randint(-15, 20)
        adjusted_min = hour * 60 + minute + jitter
        adjusted_min = max(8 * 60, min(23 * 60 + 50, adjusted_min))

        sessions.append({
            "connect_at_hour":   adjusted_min // 60,
            "connect_at_minute": adjusted_min % 60,
            "actions": actions,
            "skipped": False,
        })

    actual_comments = sum(
        1 for s in sessions
        for a in s.get("actions", [])
        if a.get("type") == "smart_comment"
    )

    return {
        "account_id":    account_id,
        "campaign_id":   campaign_id,
        "personality":   personality.get("name", "unknown"),
        "timing":        timing.get("name", "unknown"),
        "style":         style.get("name", "unknown"),
        "mood":          mood["name"],
        "day_number":    day_number,
        "total_comments": actual_comments,
        "total_sessions": len(sessions),
        "sessions":      sessions,
    }
