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
import os

# Вес «нулевой» сессии при раскладке комментов (см. ниже). Было жёстко 25.
SESSION_ZERO_WEIGHT = int(os.getenv("SESSION_ZERO_WEIGHT", "10"))
from datetime import datetime


# ═══════════════════════════════════════════════════════════
# РАСПРЕДЕЛЕНИЕ КОММЕНТАРИЕВ
# ═══════════════════════════════════════════════════════════

def distribute_comments_by_days(total_comments, total_days, even=False):
    """
    Распределяет комменты по дням кампании.

    Два режима:
      even=False (default, для regular): "прогревная" волна — 0-25% в дн 1-2,
                                          затем 40-150%, к концу снижается.
                                          Имитирует органическую активность.
      even=True (для check_only и любых screening/уже-прогретых кампаний):
                                          РАВНОМЕРНО ± небольшой jitter (±10%).
                                          Юзеру важно чтобы каждый день делалось
                                          примерно одинаково много.
    """
    if total_days <= 0:
        return []

    if even:
        # Равномерно с ощутимым разбросом: каждому дню — случайный
        # коэффициент 0.65-1.35 (примерно ±30% от среднего), потом
        # нормализуем чтобы сумма = total_comments.
        # Пример: для total=1948, days=4 → 400, 600, 350, 550-ish.
        factors = [random.uniform(0.65, 1.35) for _ in range(total_days)]
        fsum = sum(factors)
        daily = [max(0, round(total_comments * f / fsum)) for f in factors]
        # Rounding может дать погрешность — правим чтобы сумма точно
        # совпадала с total_comments.
        diff = total_comments - sum(daily)
        while diff != 0:
            idx = random.randint(0, total_days - 1)
            if diff > 0:
                daily[idx] += 1
                diff -= 1
            elif daily[idx] > 1:
                daily[idx]  -= 1
                diff += 1
        # 2026-07-24 — если последний день "маленький" (< 40% от среднего),
        # переносим его в предпоследний. Не оставляем "хвост" из 77 комментов
        # когда в предпоследнем 300 — нет смысла тянуть день ради 15% плана.
        _avg = total_comments / max(1, total_days)
        while len(daily) >= 2 and daily[-1] < 0.4 * _avg:
            daily[-2] += daily[-1]
            daily.pop()
        return daily

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


def distribute_day_comments(comments_today, accounts_with_personalities, target_by_acc=None):
    """
    Распределяет дневную квоту по аккаунтам.
    Возвращает {account_id: количество_комментов}

    Если target_by_acc={acc_id: n_channels_this_acc} передан — используем
    ПРОПОРЦИОНАЛЬНОЕ распределение (для check_only где нужно ровно
    n_channels/total_days комментов на акка, без урезания по personality).
    """
    # ── EVEN MODE (для check_only): пропорционально assigned каналам ──
    if target_by_acc is not None and any(v > 0 for v in target_by_acc.values()):
        total_target = sum(target_by_acc.values()) or 1
        distribution = {}
        remaining = comments_today
        # Первый проход: пропорционально
        for acc_id, _p in accounts_with_personalities:
            t = target_by_acc.get(acc_id, 0)
            if t <= 0:
                distribution[acc_id] = 0
                continue
            # Доля этого акка от общего пула
            share = comments_today * t / total_target
            # Округляем вниз, остаток разбрасываем
            distribution[acc_id] = min(t, int(share))
            remaining -= distribution[acc_id]
        # Разбрасываем остаток (округления) — приоритет акам где ещё не хватает
        while remaining > 0:
            cand = [a for a, c in distribution.items() if c < target_by_acc.get(a, 0)]
            if not cand:
                break
            a = random.choice(cand)
            distribution[a] += 1
            remaining -= 1
        return distribution

    # ── Классический режим (regular): по personality ──
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
        # rest = реальный пользователь иногда вообще не открывает Telegram день-два.
        # ~5% дней (≈1 раз в 3 недели) — выглядит органично, маскирует "идеально-постоянного" бота.
        {"name": "rest",    "session_mult": 0.0, "action_mult": 0.0, "weight": 5},
        {"name": "lazy",    "session_mult": 0.7, "action_mult": 0.6, "weight": 17},
        {"name": "tired",   "session_mult": 0.8, "action_mult": 0.7, "weight": 17},
        {"name": "normal",  "session_mult": 1.0, "action_mult": 1.0, "weight": 30},
        {"name": "active",  "session_mult": 1.2, "action_mult": 1.3, "weight": 17},
        {"name": "hyper",   "session_mult": 1.5, "action_mult": 1.6, "weight": 9},
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
    commented_from_prev_days=None,    # каналы где уже был коммент в предыдущих днях (для check_only 1 per channel)
    decoy_per_day=1,                  # макс. decoy-подписок в день (популярные каналы, БЕЗ комментов)
    add_tail_verify_session=False,    # +1 verify-only сессия через ~40 мин после последней
    max_comments_per_channel_per_day=1,  # 1 = 1 коммент на канал/день (check_only), 2-3 = regular с multiply
    session_times=None,               # готовые времена [(hour, minute), ...] от ip_scheduler
    first_session_comments=False,     # разрешить комменты в 1-й сессии дня (прогретые акки)
):
    """
    Генерирует УНИКАЛЬНЫЙ план дня для одного аккаунта.

    Органическая воронка:
    - В session[i] аккаунт вступает в channels_to_join[i] (если есть).
    - Commentable для session[i] = commentable_from_prev_days + каналы, вступление
      в которые произошло в session[0..i-1] ЭТОГО же дня.
    - smart_comment ставится ТОЛЬКО в commentable каналах.

    Decoy-подписки:
    - 0..decoy_per_day подписок в день на СЛУЧАЙНЫЕ популярные каналы.
    - В эти каналы НИКОГДА не комментируем — чистый шум для Telegram.
    - Эмулирует реального юзера который подписывается на разное, не только
      на то где пишет. Ломает паттерн "подписан только туда где комменчу".
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

    # ── REST DAY: целый день без активности (~5% дней) ─────
    # Только если на день нет критичных задач (комменты, вступления).
    # Если есть — конвертируем в lazy, потому что rest сломает кампанию.
    if mood["name"] == "rest":
        has_critical = bool(channels_to_join) or comments_today > 0
        if has_critical:
            mood = {"name": "lazy", "session_mult": 0.7, "action_mult": 0.6, "weight": 0}
        else:
            now = datetime.utcnow()
            return {
                "account_id":     account_id,
                "campaign_id":    campaign_id,
                "personality":    p_name,
                "timing":         timing.get("name", "unknown"),
                "style":          style.get("name", "unknown"),
                "mood":           "rest",
                "day_number":     day_number,
                "total_comments": 0,
                "total_sessions": 1,
                # День отдыха: единственная сессия помечена skipped, до
                # подключения дело не дойдёт. Флаг ставим для консистентности,
                # чтобы dispatch не считал такой план «старым» (иначе он
                # попадал бы в ветку с большим stagger'ом).
                "ip_scheduled":   True,
                "sessions": [{
                    "connect_at_hour":   (now.hour + 3) % 24,
                    "connect_at_minute": now.minute,
                    "actions":           [],
                    "skipped":           True,
                    "skip_reason":       random.choice([
                        "выходной", "не до телефона",
                        "поездка", "семейный день",
                        "проспал весь день", "забыл про телегу",
                    ]),
                }],
            }

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

    # ── Времена пришли снаружи (utils/ip_scheduler) ──────────
    # 2026-08-22: время сессий больше не выбирается генератором в одиночку.
    # Раньше каждый аккаунт рандомил себе часы независимо, ничего не зная
    # о соседях по IP — отсюда 37.5% пар подключений на одном IP в пределах
    # 15 минут, которые потом разгребал ip_throttle откладыванием (и терял
    # сессии: 126 smart_comment из 285 за 14 июля).
    #
    # Теперь ip_scheduler раскладывает сессии ВСЕХ аккаунтов одного IP на
    # общей оси с гарантированным зазором. Времена при этом остаются
    # случайными — они разные каждый день, просто больше не пересекаются.
    # ВАЖНО: отличаем None («планировщик не звали») от [] («звали, но мест
    # в остатке суток не осталось»). Раньше тут стояло bool(session_times),
    # и пустой список молча включал локальную генерацию — аккаунт получал
    # несогласованные времена и садился в чужой зазор. Стресс-тест на 25
    # кампаний поймал 5 таких случаев, все с ip_scheduled=False.
    _external_times = session_times is not None
    if _external_times:
        session_times = sorted((int(h), int(m)) for h, m in session_times)
        num_sessions = len(session_times)

    # 2026-07-23 — правило «2 гарантированные ночные сессии каждому» ОТМЕНЕНО
    # (2026-08-22, по запросу юзера). Именно оно делало 00-05 самыми
    # загруженными часами суток (37% всей активности) и создавало затор
    # в самом узком окне. Ночная активность теперь берётся из хронотипа
    # аккаунта в ip_scheduler: ночью работают только «совы».
    # Ветка ниже — fallback для вызовов БЕЗ ip_scheduler (старые пути).
    NIGHT_HOURS = [0, 1, 2, 3, 4, 5]
    N_NIGHT_SESSIONS = 2

    if not _external_times:
        # ── Локальная генерация времён (fallback) ──────────
        # Работает когда ip_scheduler не задействован: старые вызовы,
        # ручной прогрев, тесты. Соседей по IP не учитывает.
        num_sessions = max(num_sessions, N_NIGHT_SESSIONS + 3)

        session_times = []
        _night_slots = random.sample(NIGHT_HOURS, min(N_NIGHT_SESSIONS, len(NIGHT_HOURS)))
        for h in _night_slots:
            session_times.append((h, random.randint(0, 59)))

        # Оставшиеся дневные — по personality
        _day_sessions_needed = num_sessions - len(session_times)
        if p_name == "night_owl":
            for _ in range(_day_sessions_needed):
                if random.random() < 0.7:
                    session_times.append((random.randint(18, 23), random.randint(0, 59)))
                else:
                    session_times.append((random.randint(10, 16), random.randint(0, 59)))
        elif p_name == "lurker":
            for _ in range(_day_sessions_needed):
                if random.random() < 0.55:
                    session_times.append((random.randint(19, 23), random.randint(0, 59)))
                else:
                    session_times.append((random.randint(8, 18), random.randint(0, 59)))
        else:
            for _ in range(_day_sessions_needed):
                r = random.random()
                if r < 0.35:
                    session_times.append((random.randint(8, 13), random.randint(0, 59)))
                elif r < 0.65:
                    session_times.append((random.randint(13, 18), random.randint(0, 59)))
                else:
                    session_times.append((random.randint(18, 23), random.randint(0, 59)))

        # Хронологическая сортировка (00 → 05 → 08 → 23).
        session_times.sort()

        min_gap = random.randint(20, 90)
        for i in range(1, len(session_times)):
            prev_min = session_times[i-1][0] * 60 + session_times[i-1][1]
            curr_min = session_times[i][0] * 60 + session_times[i][1]
            if curr_min - prev_min < min_gap:
                new_min = prev_min + min_gap + random.randint(0, 30)
                new_h = new_min // 60
                new_m = new_min % 60
                if new_h > 23:
                    new_h = 23
                    new_m = 50
                session_times[i] = (new_h, new_m)

    # Для внешних времён отсев прошедших слотов уже сделан в ip_scheduler
    # (параметр not_before_min), причём с учётом соседей по IP. Повторять
    # его здесь нельзя: локальный fallback «сейчас + 15 мин» выдавал ОДНО
    # И ТО ЖЕ время всем аккаунтам одного IP и создавал ровно то совпадение,
    # от которого мы уходим (поймано E2E-тестом 2026-08-22: acc#209 и
    # acc#210 оба получили 18:06).
    if day_number == 1 and not _external_times:
        # Разрешаем только БУДУЩИЕ сессии по календарю сегодня.
        # Ночные (h<6) разрешены ТОЛЬКО если сейчас тоже ночь (current_hour<6),
        # иначе они "уже прошли утром" и dispatch их пропустит как просроченные.
        # Пример: сейчас 23:12 → сессия 03:00 уже прошла (23-3=20ч > 4ч лимит).
        def _is_future(h, m):
            if 6 <= current_hour <= 23:
                # Дневное сейчас — только час больше текущего
                return h > current_hour or (h == current_hour and m >= min_start_minute)
            else:
                # Ночь сейчас (0-5) — ночные больше текущего или дневные
                if h < 6:
                    return h > current_hour or (h == current_hour and m >= min_start_minute)
                return True   # любое дневное = будущее (уже сегодня после 06:00)
        session_times = [(h, m) for h, m in session_times if _is_future(h, m)]
        if not session_times:
            next_h = current_hour
            next_m = current_minute + 15
            if next_m >= 60:
                next_h += 1
                next_m -= 60
            if next_h <= 23:
                session_times = [(next_h, next_m)]

    # ── Tail-verify: одна verify-only сессия через 35-45 мин после последней
    # рабочей. Только на последнем дне кампании (add_tail_verify_session=True).
    # Индекс этой сессии сохраняем в tail_sess_idx — цикл ниже отдельно
    # обработает: без join, без комментов, только verify + пара read_feed.
    tail_sess_idx = -1
    if add_tail_verify_session and session_times:
        if _external_times:
            # ── 2026-08-22 ─────────────────────────────────────────
            # Придумывать новое время нельзя: оно не согласовано с
            # соседями по IP и почти наверняка попадёт в чужой зазор.
            # E2E-тест поймал именно это: tail-сессия acc#208 встала в
            # 23:53, а у acc#209 уже было 23:46 — интервал 7 минут.
            # Вместо этого caller запрашивает на последний день на одну
            # сессию больше, и последняя из выданных становится
            # verify-only. Она и так позже всех рабочих.
            if len(session_times) >= 2:
                tail_sess_idx = len(session_times) - 1
        else:
            last_h, last_m = session_times[-1]
            delay = random.randint(35, 45)
            total_min = last_h * 60 + last_m + delay
            new_h = total_min // 60
            new_m = total_min % 60
            if new_h <= 23:                     # если не переползли за полночь
                session_times.append((new_h, new_m))
                tail_sess_idx = len(session_times) - 1

    # ═══════════════════════════════════════════════════
    # ЦИКЛ СЕССИЙ — органическая воронка подписок
    # ═══════════════════════════════════════════════════

    joined_today   = []         # каналы, вступление в которые произошло СЕГОДНЯ
    remaining_comments = comments_today
    sessions = []
    # Каналы для которых уже запланирован join_discussion_of в предыдущих
    # или текущей сессии. Перед КАЖДЫМ smart_comment @X проверяем: если X
    # тут ещё не был — вставляем join_discussion_of @X ПЕРЕД smart_comment
    # (executor всё равно skip'нет коммент если Redis-флаг disc_joined не
    # установлен). Этот tracker гарантирует что план это учитывает.
    _disc_scheduled: set = set()
    # Каналы куда УЖЕ запланирован smart_comment в этом плане ИЛИ в
    # предыдущих днях. Правило: 1 коммент на канал в день (органично +
    # для check_only это ЖЁСТКОЕ требование "1 коммент на канал ВООБЩЕ",
    # поэтому caller передаёт `commented_from_prev_days` для кросс-day дедупа).
    # Counter вместо set — считаем сколько раз канал уже комментировался
    # (позволяет несколько комментов в один канал за день если max_per_ch > 1)
    from collections import Counter as _Counter
    _commented_channels: _Counter = _Counter(commented_from_prev_days or [])
    _max_per_ch = max(1, int(max_comments_per_channel_per_day))

    for sess_i, (hour, minute) in enumerate(session_times):

        # Что commentable СЕЙЧАС (до действий этой сессии)
        sess_commentable = commentable_start + joined_today

        # Вступаем ли в канал в этой сессии?
        # На tail-verify сессии — НЕ вступаем (это по определению verify-only).
        _is_tail_verify_sess = (sess_i == tail_sess_idx)
        if _is_tail_verify_sess:
            join_this_sess = None
        else:
            join_this_sess = channels_to_join[sess_i] if sess_i < len(channels_to_join) else None
        if join_this_sess:
            joined_today.append(join_this_sess)

        # ── Сколько комментов в этой сессии ─────────────
        # На tail-verify — 0 комментов, 0 реакций.
        sessions_left = len(session_times) - sess_i
        sess_c_count = 0
        # check_only: агрессивно распределяем ВСЕ comments_today (иначе
        # personality-random режет до 25% зeroed sessions + `sess_i>=1`
        # блокирует 1-ю сессию → в итоге ~50% плана не помещается).
        _force_all_comments = personality.get("_force_verify_every_session", False)
        # Первая сессия дня обычно комментов не пишет — аккаунт «зашёл,
        # осмотрелся». Для ПРОГРЕТЫХ аккаунтов (уже подписаны на свои каналы)
        # это лишняя трата: замер 25.08 — при 3.6 сессии в день блокировка
        # первой съедала около четверти дневных слотов, а новых подключений
        # разрешение не добавляет, аккаунт и так уже в сети.
        _can_comment_this_sess = (
            sess_i >= 1 or _force_all_comments or first_session_comments
        )
        if not _is_tail_verify_sess and remaining_comments > 0 and sess_commentable and _can_comment_this_sess:
            # sessions_left исключая tail_verify (там 0 комментов)
            _work_sessions_left = sessions_left - (1 if tail_sess_idx >= 0 and sess_i < tail_sess_idx else 0)
            _work_sessions_left = max(1, _work_sessions_left)
            if _force_all_comments:
                # check_only: math.ceil чтобы гарантированно распределить ВСЁ.
                import math as _m
                sess_c_count = _m.ceil(remaining_comments / _work_sessions_left)
            else:
                avg = max(1, remaining_comments / _work_sessions_left)
                # Доля «пустых» сессий. Держит поведение неровным, но 25%
                # при трёх-четырёх сессиях в день выбрасывали слишком много
                # слотов. Вынесено в env, чтобы крутить без правки кода.
                sess_c_count = random.choices(
                    [0, max(1, round(avg)), max(1, round(avg)) + 1],
                    weights=[SESSION_ZERO_WEIGHT, 50, 25], k=1
                )[0]
            # для check_only — лимит 8 в сессию (было 3),
            # чтобы вместить пиковые дни при много каналов/акка
            # regular: 6 комментов/сессию (было 3) — чтобы multiply 2-3 коммента
            # в один канал влезал. check_only остаётся 8 как раньше.
            _max_per_sess = 8 if _force_all_comments else 6
            sess_c_count = min(sess_c_count, _max_per_sess, remaining_comments)

        remaining_comments = max(0, remaining_comments - sess_c_count)

        # ── Цели комментариев: ТОЛЬКО commentable И ещё не прокомментированные ──
        # 1 коммент на канал в день (для check_only это гарантирует
        # "1 коммент на канал ВООБЩЕ" — caller исключает already-commented
        # каналы из campaign_channels при генерации следующих дней).
        sess_c_targets = []
        if not _is_tail_verify_sess and sess_c_count > 0 and sess_commentable:
            _uncommented = [ch for ch in sess_commentable if _commented_channels[ch] < _max_per_ch]
            if _uncommented:
                sess_c_targets = random.sample(
                    _uncommented,
                    min(sess_c_count, len(_uncommented)),
                )
            # Если все каналы уже прокомментированы — возвращаем quota обратно
            else:
                remaining_comments += sess_c_count
                sess_c_count = 0

        # ── Реакции: тоже только в commentable ──────────
        sess_r_targets = []
        if not _is_tail_verify_sess and sess_commentable and random.random() < 0.5:
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
                # post_story — низкий вес, и только если у акка есть фото в папке
                # (исполнитель сам делает skip если папка пустая или акк не Premium)
                ("post_story", 3),
            ]
        else:
            available_actions = [
                ("read_feed", 28), ("view_stories", 12),
                ("set_reaction", 15), ("view_profile", 6),
                ("search", 5), ("forward_saved", 7),
                ("send_saved", 10), ("reply_dm", 7),
                ("typing", 4),
                ("post_story", 4),
            ]

        # ── Строим список действий ───────────────────────
        actions = []

        # 0. VERIFY: ОДИН точный ping в СЛЕДУЮЩЕЙ сессии после отправки.
        # Действие verify_my_comments проверяет только комменты старше
        # min_age_sec (1800 сек = 30 мин) — значит комменты этой же сессии
        # НЕ верифицируются (они слишком свежие), а верифятся комменты
        # предыдущих сессий (в т.ч. с ПРЕДЫДУЩЕГО ДНЯ — window_hours=24
        # покрывает и они уже старше 30 мин).
        #
        # Cadence: КАЖДАЯ сессия (включая первую сессию дня — там могут
        # быть неверифицированные комменты с последней сессии прошлого дня).
        # Executor сам early-exit'ит если нечего проверять ("непроверенных
        # нет") — cost = 1 SQL SELECT, безопасно.
        actions.append({
            "type": "verify_my_comments",
            "window_hours": 24,             # смотрим комменты за последние 24ч
            "min_age_sec": 1800,            # НО не свежее 30 мин
            "max_checks": 20 if not personality.get("_force_verify_every_session") else 40,
            "pause_after": random.randint(15, 45),
        })

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
                    # ── ГАРАНТ: перед smart_comment @X должен быть
                    # join_discussion_of @X в этом или предыдущем сессионе.
                    # Иначе executor тупо skip'нет smart_comment.
                    if target not in _disc_scheduled:
                        actions.append({
                            "type": "join_discussion_of",
                            "channel": target,
                            "pause_after": random.randint(4, 15),
                        })
                        _disc_scheduled.add(target)
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
                    _commented_channels[target] += 1
                    # ── Реакции на ЧУЖИЕ комменты ───────────────────────
                    # Это отдельный канал трафика: автор коммента получает
                    # уведомление и заходит смотреть, кто отреагировал. В
                    # отличие от set_reaction, которая ставится на пост канала
                    # и никому уведомления не шлёт.
                    #
                    # Было: 15% на две реакции и 25% на одну, всегда в тот же
                    # канал, куда написали. Стало: половина случаев, от одной
                    # до трёх, и иногда веером по нескольким каналам.
                    if random.random() < 0.50:
                        actions.append({
                            "type": "react_to_comment",
                            "channel": target,
                            "count": random.randint(1, 3),
                            "pause_after": random.randint(3, 12),
                        })
                        # ── Веер по соседним каналам ────────────────────
                        # Берём ТОЛЬКО те, где аккаунт уже вступил в
                        # обсуждение в этом плане: такой канал лежит в кэше
                        # сессии, и обращение к нему не тратит резолв. А
                        # именно резолвы приводят к флуду — незнакомое имя
                        # стоит запроса ResolveUsername, и на них выгорали
                        # аккаунты при обходе каналов.
                        _extra_pool = [ch for ch in _disc_scheduled if ch != target]
                        if _extra_pool and random.random() < 0.35:
                            _n_extra = min(random.randint(1, 2), len(_extra_pool))
                            for _ch in random.sample(_extra_pool, _n_extra):
                                actions.append({
                                    "type": "react_to_comment",
                                    "channel": _ch,
                                    "count": random.randint(1, 2),
                                    "pause_after": random.randint(8, 30),
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
            elif chosen == "post_story":
                # Просто пауза после — executor сам решит публиковать или скипнуть
                action["pause_after"] = random.randint(15, 60)

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

        # ── DISCUSSION-JOIN SPRINKLE ────────────────────────
        # С шансом ~40% в эту сессию — вставляем join_discussion_of
        # для случайного канала из commentable (тот в который уже
        # вступил ранее, но возможно ещё не в его discussion).
        # Executor молча съедает UserAlreadyParticipant, так что
        # повторение не вредит. За 3-4 дня legacy 900 доподпишутся
        # равномерно через нормальный flow. ── А в сессиях где сейчас
        # уже был join_target_channel — НЕ ставим (иначе бандлинг).
        # Sprinkle: НЕ те что уже в _disc_scheduled, приоритет "ещё не запланировано"
        _disc_pool_all = list(commentable_start + joined_today[:-1] if join_this_sess else commentable_start + joined_today)
        _disc_pool = [ch for ch in _disc_pool_all if ch not in _disc_scheduled]
        if _disc_pool and not _is_tail_verify_sess and random.random() < 0.45:
            _dch = random.choice(_disc_pool)
            # Вставляем в СЛУЧАЙНОЕ место среди actions (не первым и не
            # последним — иначе выглядит как «зашёл и сразу подписался»).
            _insert_at = random.randint(1, max(1, len(actions) - 1)) if len(actions) >= 3 else len(actions)
            actions.insert(_insert_at, {
                "type": "join_discussion_of",
                "channel": _dch,
                "pause_after": random.randint(3, 12),
            })
            _disc_scheduled.add(_dch)

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

        if _external_times:
            # Время пришло от ip_scheduler и уже согласовано с соседями по
            # IP. Любой jitter здесь съел бы гарантированный зазор: сдвиг
            # ±20 мин легко превращает 22-минутный интервал в 2-минутный.
            # Разброс уже заложен внутри планировщика.
            adjusted_min = hour * 60 + minute
        else:
            jitter = random.randint(-15, 20)
            adjusted_min = hour * 60 + minute + jitter
            # 2026-07-23 — старый код клампил всё в 8:00-23:50, уничтожая
            # ночные сессии (0-5). Теперь: для ночных (h<6) не трогаем,
            # для дневных клампим 8:00-23:50 как раньше.
            if hour < 6:
                # Ночная — jitter в узком диапазоне ±10 мин, не переходя за 05:59
                adjusted_min = max(0, min(5 * 60 + 59, adjusted_min))
            else:
                adjusted_min = max(8 * 60, min(23 * 60 + 50, adjusted_min))

        sessions.append({
            "connect_at_hour":   adjusted_min // 60,
            "connect_at_minute": adjusted_min % 60,
            "actions": actions,
            "skipped": False,
        })

    # ── DECOY-подписки: 0..decoy_per_day на популярные каналы ────
    # Подписываемся на случайные нейтральные каналы, в которые НЕ комментим.
    # Это шум: реальный юзер подписан не только туда где пишет.
    # Смещение к 0-1 (не каждый день человек подписывается на новое).
    if decoy_per_day > 0:
        decoy_pool = list(range(decoy_per_day + 1))
        decoy_weights = [55, 32, 13, 7, 4][:len(decoy_pool)]
        decoy_budget = random.choices(decoy_pool, weights=decoy_weights, k=1)[0]

        if decoy_budget > 0:
            # Только в непропущенные сессии с действиями, не более 1 decoy на сессию
            eligible = [s for s in sessions if not s.get("skipped") and s.get("actions")]
            random.shuffle(eligible)
            for s in eligible[:decoy_budget]:
                pos = random.randint(0, len(s["actions"]))
                s["actions"].insert(pos, {
                    "type": "join_channel",   # executor джойнит случайный популярный канал
                    "decoy": True,            # пометка: НЕ создаём assignment, НЕ комментим
                    "pause_after": random.randint(20, 90),
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
        # Времена согласованы по IP (utils/ip_scheduler) — dispatch_plans
        # обязан стартовать такие планы почти точно, без большого stagger'а.
        "ip_scheduled":  _external_times,
        "sessions":      sessions,
    }
