"""
prep_plan.py — Планировщик и executor для «подготовки» аккаунтов
как отдельных планов по образу plan_executor.

Отличия от prepare_accounts_bulk (простого режима):

  Простой режим:
    for acc in accounts: connect → все действия сразу → disconnect
    Между акками — пауза X сек.
    Всё последовательно.

  План-режим:
    Для каждого акка генерится РАСПИСАНИЕ из N сессий, каждая в своём
    временном окне внутри target_duration_hours. Действия РАЗБИТЫ по
    сессиям в разном порядке (акк A: сессия 1 = канал, сессия 2 = имя,
    сессия 3 = аватарки; акк B: сессия 1 = аватар профиля, сессия 2 =
    канал+аватар канала, сессия 3 = имя+первый пост).

    Каждая сессия — отдельный Celery task с countdown до своего момента.
    Разные акки (на разных прокси) выполняются ПАРАЛЛЕЛЬНО через
    Celery workers. Один прокси одновременно = один акк через ip_lock.

Хранение состояния:
    Redis:
      prep_plan:{job_id}:meta         — общий meta (config, total, ts)
      prep_plan:{job_id}:accounts     — set: account_id → per-acc plan JSON
      prep_plan:{job_id}:progress     — агрегированный snapshot для UI
"""

from __future__ import annotations
import json
import random
import time
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


# ── Atomic actions ────────────────────────────────────────────────
#
# Каждый action = один атомарный шаг. Юзер может включить/выключить
# группы (avatars/names/channels), а генератор распаковывает их в
# отдельные action'ы:
#
#   change_profile_name    — UpdateProfileRequest(first_name, last_name)
#   set_profile_avatar     — UploadProfilePhotoRequest
#   create_channel         — CreateChannelRequest (title, description)
#   set_channel_avatar     — EditPhotoRequest (требует прошлого create_channel)
#   pin_channel_to_profile — ToggleDialogPinRequest (требует create_channel)
#   post_first_message     — send_message (требует create_channel,
#                            рекомендуется в отдельной сессии от create)

ACTION_TYPES = {
    "change_profile_name",
    "set_profile_avatar",
    "create_channel",
    "set_channel_username",
    "set_channel_avatar",
    "pin_channel_to_profile",
    "post_first_message",
}


def _random_time_in_window(start_ts: float, end_ts: float) -> float:
    """Случайный момент в окне."""
    if end_ts <= start_ts:
        return start_ts
    return random.uniform(start_ts, end_ts)


def _split_into_sessions(actions: list[dict], n_sessions: int) -> list[list[dict]]:
    """Распределяет action'ы по сессиям с уважением зависимостей.

    Зависимости:
      set_channel_avatar        after create_channel (можно в той же сессии)
      pin_channel_to_profile    after create_channel (в той же сессии — так надёжнее)
      post_first_message        after create_channel, но ОБЯЗАТЕЛЬНО в
                                другой сессии (не той в которой create'нули)
                                — это ключ! Флуд-фильтр Telegram душит посты
                                на свежесозданном канале если они идут в
                                том же коннекте. Разные сессии = разные
                                коннекты = разные времена.
    """
    result: list[list[dict]] = [[] for _ in range(max(1, n_sessions))]
    if not actions:
        return result

    # 1. Достаём action'ы каналов — их зависимости
    create_ch = next((a for a in actions if a["type"] == "create_channel"), None)
    ch_username = next((a for a in actions if a["type"] == "set_channel_username"), None)
    ch_avatar = next((a for a in actions if a["type"] == "set_channel_avatar"), None)
    ch_pin    = next((a for a in actions if a["type"] == "pin_channel_to_profile"), None)
    ch_post   = next((a for a in actions if a["type"] == "post_first_message"), None)

    # 2. Остальные "свободные" action'ы (профиль-действия)
    free_actions = [a for a in actions
                    if a["type"] in ("change_profile_name", "set_profile_avatar")]
    random.shuffle(free_actions)

    # 3. Раскладываем свободные равномерно
    for i, a in enumerate(free_actions):
        result[i % n_sessions].append(a)

    # 4. Канал: create + avatar + pin в одну сессию (первая или средняя,
    #    но НЕ последняя — чтоб post ушёл после)
    if create_ch:
        # Выбираем «канал-сессию» рандомно из первых (n_sessions - 1)
        # чтобы post_first_message имел куда попасть после.
        max_ch_session = max(0, n_sessions - 2) if ch_post else n_sessions - 1
        ch_session_idx = random.randint(0, max_ch_session)
        result[ch_session_idx].append(create_ch)
        if ch_username:
            # username ДОЛЖЕН быть в той же сессии (нужен канал в ctx)
            result[ch_session_idx].append(ch_username)
        if ch_avatar:
            result[ch_session_idx].append(ch_avatar)
        if ch_pin:
            result[ch_session_idx].append(ch_pin)
        # 5. Первый пост — в СЛЕДУЮЩЕЙ или ЛЮБОЙ более поздней сессии
        if ch_post:
            post_min = ch_session_idx + 1
            post_max = n_sessions - 1
            if post_min <= post_max:
                post_idx = random.randint(post_min, post_max)
                result[post_idx].append(ch_post)
            else:
                # Пограничный случай — падаем в последнюю
                result[-1].append(ch_post)

    # 6. Перемешиваем порядок action'ов внутри каждой сессии
    # (создание канала не первым обязательно)
    for sess in result:
        # НО create_channel должен идти ДО зависимых в той же сессии
        deps_after = {"set_channel_username", "set_channel_avatar", "pin_channel_to_profile"}
        has_create = any(a["type"] == "create_channel" for a in sess)
        if has_create:
            # Сортируем: create_channel первым если он есть в этой сессии
            sess.sort(key=lambda a: 0 if a["type"] == "create_channel" else 1)
            # но зависимые за ним — рандомно
            # (уже они в конце после create — просто перемешаем хвост)
            head, tail = sess[:1], sess[1:]
            random.shuffle(tail)
            sess.clear()
            sess.extend(head + tail)
        else:
            random.shuffle(sess)
    return result


def build_actions_for_account(
    account_id: int,
    change_names: bool,
    names_pool: list[dict],
    change_avatars: bool,
    profile_avatar_paths: list[str],
    create_channels: bool,
    channel_titles: list[str],
    channel_descriptions: list[str],
    channel_first_post: str,
    channel_avatar_paths: list[str],
    channel_post_photo_paths: list[str],
    channel_pin: bool,
    delete_previous_profile_avatar: bool = False,
    delete_previous_channel_avatar: bool = False,
    custom_posts_by_acc: dict | None = None,
    channel_public: bool = False,
    channel_public_usernames: list[str] | None = None,
) -> list[dict]:
    """Собирает список атомарных action'ов для конкретного акка,
    рандомизируя ресурсы (случайное имя из pool, случайное фото и т.д.)."""
    actions = []

    if change_names and names_pool:
        n = random.choice(names_pool)
        actions.append({
            "type": "change_profile_name",
            "first_name": n.get("first_name", ""),
            "last_name": n.get("last_name", ""),
        })

    if change_avatars and profile_avatar_paths:
        actions.append({
            "type": "set_profile_avatar",
            "avatar_path": random.choice(profile_avatar_paths),
            "delete_previous": bool(delete_previous_profile_avatar),
        })

    if create_channels and channel_titles:
        title = random.choice(channel_titles)
        description = random.choice(channel_descriptions) if channel_descriptions else ""
        ch_avatar = random.choice(channel_avatar_paths) if channel_avatar_paths else None
        actions.append({
            "type": "create_channel",
            "title": title,
            "description": description,
        })
        # Публичный канал — сразу после create ставим username (в той же сессии).
        # _split_into_sessions уже соблюдает: set_channel_username остаётся
        # там же где create_channel (мы явно проставим его после create в
        # результате split'а — см. sess.sort по create_channel first).
        if channel_public and channel_public_usernames:
            uname = random.choice(channel_public_usernames)
            actions.append({
                "type": "set_channel_username",
                "username_candidates": [uname],
            })
        if ch_avatar:
            actions.append({
                "type": "set_channel_avatar",
                "avatar_path": ch_avatar,
                "delete_previous": bool(delete_previous_channel_avatar),
            })
        if channel_pin:
            actions.append({
                "type": "pin_channel_to_profile",
            })
        # Post first message: приоритет — custom по acc_id, потом common+random.
        custom = (custom_posts_by_acc or {}).get(str(account_id))
        if custom:
            post_text = custom.get("text") or ""
            post_photo = custom.get("photo_path")
        else:
            post_text = channel_first_post or ""
            post_photo = random.choice(channel_post_photo_paths) if channel_post_photo_paths else None
        if (post_text and post_text.strip()) or post_photo:
            actions.append({
                "type": "post_first_message",
                "text": post_text,
                "photo_path": post_photo,
            })
    return actions


def generate_plan(
    account_id: int,
    phone: str,
    actions: list[dict],
    target_start_ts: float,
    target_duration_sec: int,
    n_sessions: int,
) -> dict:
    """Генерирует план для одного акка.

    Расписание: n_sessions равных окон внутри target_duration.
    Каждое окно — [k * seg .. (k+1) * seg], случайный момент внутри.

    Первая сессия может стартовать через 0..seg_size сек — не всегда сразу.
    Между сессиями естественная разница ≥ 60 сек (иначе на пороге окна
    может выпасть очень близко).

    Действия по сессиям — через _split_into_sessions (см. выше).
    """
    if not actions:
        return {
            "account_id": account_id, "phone": phone,
            "sessions": [], "created_at": int(target_start_ts),
        }

    # Реальное количество сессий не больше числа action'ов
    n_sessions = max(1, min(int(n_sessions), len(actions)))

    session_actions_lists = _split_into_sessions(actions, n_sessions)

    # Фильтруем пустые сессии (могут остаться после split)
    filled = [(i, acts) for i, acts in enumerate(session_actions_lists) if acts]
    n_sessions_effective = len(filled)

    seg_size = target_duration_sec / n_sessions_effective
    sessions = []
    last_ts = 0
    # Минимум между сессиями. Для коротких прогонов (5-10 мин на всё)
    # 60 сек слишком много — упирается в общий бюджет.
    min_gap_sec = min(60.0, seg_size * 0.5)
    for k, (orig_idx, acts) in enumerate(filled):
        window_start = target_start_ts + k * seg_size
        window_end = target_start_ts + (k + 1) * seg_size
        if k == 0:
            # ПЕРВАЯ сессия — bias к самому началу окна (0-25% от первого сегмента).
            # Юзер не должен ждать половину дедлайна прежде чем увидит первую активность.
            connect_ts = target_start_ts + random.uniform(0, seg_size * 0.25)
        else:
            connect_ts = _random_time_in_window(window_start, window_end)
        # Гарантируем минимум min_gap_sec с прошлой сессии
        if last_ts > 0 and (connect_ts - last_ts) < min_gap_sec:
            connect_ts = last_ts + min_gap_sec + random.uniform(0, min_gap_sec * 0.3)
        last_ts = connect_ts
        sessions.append({
            "idx": k,
            "connect_ts": int(connect_ts),
            "actions": acts,
            "status": "pending",   # pending | scheduled | running | done | failed
            "attempts": 0,
        })

    return {
        "account_id": account_id,
        "phone": phone,
        "sessions": sessions,
        "created_at": int(target_start_ts),
        "target_duration_sec": target_duration_sec,
        "n_sessions": n_sessions_effective,
    }


def recommend_duration(
    n_accounts: int,
    n_actions_per_acc: int,
) -> dict:
    """Возвращает рекомендации по target_duration для UI.

    Минимум: 5 мин — для тест-прогонов на 1-2 акках.
    Рекомендация: масштабируется с числом акков и action'ов.
    """
    n_sessions = min(4, max(1, n_actions_per_acc))
    # Минимум — константа 5 мин (0.083ч). Юзер может пробовать на паре акков.
    min_hours = round(0.08, 2)
    # Рекомендация: 5 мин на сессию + 0.5 мин на акк
    # 2 акк × 4 сессии → 20 + 1 = 21 мин
    # 25 акк × 4 сессии → 20 + 12.5 = 32 мин
    # 100 акк × 4 сессии → 20 + 50 = 70 мин
    recommended_hours = round(
        max(0.15, n_sessions * 5 / 60 + n_accounts * 0.5 / 60), 2
    )
    max_hours = 24.0
    return {
        "min_hours": min_hours,
        "recommended_hours": recommended_hours,
        "max_hours": max_hours,
    }
