"""
GramGPT — tasks/parser_multi_tasks.py

Мульти-аккаунтный парсер каналов через Telegram-поиск.

Отличия от parser_tasks.run_parser_search (single-account):
  1. Работает с NUM аккаунтов сразу.
  2. Случайное количество ОДНОВРЕМЕННЫХ воркеров: random.randint(2, min(7, N_proxies)).
     Не всегда все акки в работе — часть отдыхает. Такой рваный ритм
     менее заметен для TG anti-abuse.
  3. Один прокси = один worker (аккаунты за общим прокси идут ПОСЛЕДОВАТЕЛЬНО).
     Иначе получим ip_lock коллизии и flood.
  4. Один connect() на весь run аккаунта — не переподключаемся между
     keyword'ами. Меньше "миллионов" подключений, меньше подозрений TG.
  5. Точная проверка has_comments (GetFullChannel.linked_chat_id) — ВСЕГДА.
     Это лишний запрос, но сразу пишем правильный флаг → не нужно
     потом отдельно пере-верифицировать.
  6. FloodWait handling:
       * НЕ ретраим внутри одного акка (retry → ещё flood → ещё больше wait).
       * Ставим Redis-cooldown `parser:acc_cooldown:{acc_id}` на MAX(1 час,
         floodwait_secs + 60). Аккаунт выходит из этого run.
       * Worker берёт следующий акк из своей proxy-группы; если пусто —
         worker завершается, семафор освобождается.
     Cooldown-ключ уважают ВСЕ парсеры (perms.multi, single search, similar),
     что бы не выстрелить в тот же акк сразу после flood'а.
  7. Split ключевых слов round-robin между активными акками. Каждый акк
     получает свой уникальный диапазон → минимум пересечений результата.

Progress format (`parser:multi:progress:{user_id}:{job_id}`):
    {
      "status": "running" | "done" | "cancelled" | "error",
      "job_id": "...",
      "concurrency": 3,           # выбранное N воркеров
      "total_accounts": 10,
      "started_at": <ts>,
      "found_total": 47,          # уник каналов за весь run
      "saved_total": 32,          # новых в БД за весь run
      "accounts": {
        123: {"phone":"+X", "status":"running", "current_kw":"crypto",
              "found":5, "saved":3, "keywords_done": 4, "keywords_total": 20,
              "flood_wait":0},
        124: {"phone":"+Y", "status":"flooded", "flood_wait":3600, ...},
        ...
      },
      "ts": <ts>
    }
"""

import asyncio
import sys
import os
import time
import json
import logging
import random
from collections import defaultdict
from datetime import datetime, timedelta

from celery_app import celery_app

logger = logging.getLogger(__name__)
API_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)


# ═══════════════════════════════════════════════════════════
# Redis keys
# ═══════════════════════════════════════════════════════════

def _progress_key(user_id: int, job_id: str) -> str:
    return f"parser:multi:progress:{user_id}:{job_id}"


def _stop_key(user_id: int, job_id: str) -> str:
    return f"parser:multi:stop:{user_id}:{job_id}"


def _task_id_key(user_id: int, job_id: str = "") -> str:
    """Celery task_id — per-job, чтобы force-reset мог revoke конкретный
    воркер. Legacy формат без job_id используется в start-моменте когда
    job_id ещё генерируется в task'е — но start_endpoint передаёт job_id."""
    if job_id:
        return f"parser:multi:task_id:{user_id}:{job_id}"
    return f"parser:multi:task_id:{user_id}"


def _acc_cooldown_key(acc_id: int) -> str:
    """Общий cooldown-флаг для аккаунта после FloodWait. Уважается всеми
    парсерами. Значение = ts когда истекает (для UI). TTL авто-очищает."""
    return f"parser:acc_cooldown:{acc_id}"


def _dedup_key(user_id: int, job_id: str) -> str:
    """Redis SET usernames найденных в этом job'е — чтобы разные акки
    не сохраняли одно и то же."""
    return f"parser:multi:dedup:{user_id}:{job_id}"


# ═══════════════════════════════════════════════════════════
# Async helpers
# ═══════════════════════════════════════════════════════════

def _run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _sleep_cancellable(seconds: float, redis, stop_k: str,
                             chunk: float = 0.5) -> bool:
    """Спит N сек, каждые chunk сек проверяет stop. True = отменено."""
    remaining = float(seconds)
    while remaining > 0:
        try:
            if redis.get(stop_k):
                return True
        except Exception:
            pass
        step = min(chunk, remaining)
        await asyncio.sleep(step)
        remaining -= step
    return False


# ═══════════════════════════════════════════════════════════
# Event logging + DB helpers
# ═══════════════════════════════════════════════════════════

async def _log_event(user_id, event_type, **kwargs):
    try:
        from utils.parser_events import log_event
        await log_event(user_id=user_id, event_type=event_type, **kwargs)
    except Exception as e:
        logger.debug(f"[parser_events] skip: {e}")


async def _save_channel(user_id: int, channel_data: dict) -> bool:
    """UPSERT-ish: пропускаем если username уже есть у юзера. Возвращаем
    True если добавили новый."""
    from sqlalchemy import select
    from models.parsed_channel import ParsedChannel
    from utils.db_pool import async_session as Session

    async with Session() as db:
        existing = await db.execute(
            select(ParsedChannel).where(
                ParsedChannel.user_id == user_id,
                ParsedChannel.username == channel_data["username"],
            )
        )
        if existing.scalar_one_or_none():
            return False
        post_date = None
        if channel_data.get("last_post_date"):
            try:
                dt = datetime.fromisoformat(channel_data["last_post_date"].replace("Z", ""))
                post_date = dt.replace(tzinfo=None)
            except Exception:
                pass
        row = {
            "user_id": user_id,
            "channel_id": channel_data.get("channel_id", 0),
            "username": channel_data["username"],
            "title": channel_data.get("title", ""),
            "subscribers": channel_data.get("subscribers", 0),
            "has_comments": channel_data.get("has_comments", False),
            "last_post_date": post_date,
            "search_query": channel_data.get("search_query", ""),
        }
        if hasattr(ParsedChannel, "source"):
            row["source"] = "telegram_multi"
        db.add(ParsedChannel(**row))
        await db.commit()
        return True


# ═══════════════════════════════════════════════════════════
# Telegram helpers
# ═══════════════════════════════════════════════════════════

async def _check_has_comments(client, chat):
    """(commentable, flood_wait_seconds).

    ── 2026-08-22 ───────────────────────────────────────────────
    Проверяем не только наличие обсуждений, но и можно ли туда писать:
    из ТОГО ЖЕ ответа GetFullChannel берём join_request (вступление по
    заявке) и default_banned_rights (запрет писать). Дополнительных
    запросов ноль. Каналы «только по заявке» раньше проходили фильтр как
    годные и потом собирали отказы от каждого аккаунта по очереди.
    """
    from utils.channel_caps import check_and_record, entity_username
    # entity_username учитывает мульти-юзернеймы: у части каналов
    # поле username пустое, а имя лежит в списке usernames.
    uname = entity_username(chat)
    ok, fw, caps = await check_and_record(client, chat, uname, source="parser_multi")
    if not ok and caps.get("reason") and caps.get("reason") != "no_discussion":
        logger.info(f"[parser.multi] @{uname}: отсеян — {caps['reason']}")
    return (ok, fw)


async def _get_last_post_date(client, chat):
    from telethon.errors import FloodWaitError
    try:
        msgs = await client.get_messages(chat, limit=1)
        if msgs:
            return msgs[0].date
    except FloodWaitError:
        pass
    except Exception:
        pass
    return None


# ═══════════════════════════════════════════════════════════
# Основной multi-парсер
# ═══════════════════════════════════════════════════════════

async def _run_multi(user_id: int, account_ids: list[int], params: dict):
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload
    from models.account import TelegramAccount
    from models.proxy import Proxy
    from utils.telegram import make_telethon_client
    from utils.db_pool import async_session as Session
    from telethon.tl.functions.contacts import SearchRequest as TgSearchRequest
    from telethon.errors import FloodWaitError

    import redis as redis_lib
    redis = redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    job_id = params.get("job_id") or f"multi_{int(time.time())}_{random.randint(1000,9999)}"
    prog_k = _progress_key(user_id, job_id)
    stop_k = _stop_key(user_id, job_id)
    dedup_k = _dedup_key(user_id, job_id)
    redis.delete(stop_k)

    # ── Load accounts + proxies ──
    accs_by_id = {}
    proxies_by_id: dict[int, "Proxy"] = {}
    async with Session() as db:
        acc_r = await db.execute(
            select(TelegramAccount).options(joinedload(TelegramAccount.api_app))
            .where(
                TelegramAccount.id.in_(account_ids),
                TelegramAccount.user_id == user_id,
            )
        )
        for a in acc_r.scalars().all():
            accs_by_id[a.id] = a
        # proxies
        proxy_ids = list({a.proxy_id for a in accs_by_id.values() if a.proxy_id})
        if proxy_ids:
            pr = await db.execute(select(Proxy).where(Proxy.id.in_(proxy_ids)))
            for p in pr.scalars().all():
                proxies_by_id[p.id] = p

    # ── Отсеиваем акки на cooldown ──
    now_ts = int(time.time())
    skipped_cooldown: list[dict] = []
    active_accs = []
    for acc_id in account_ids:
        a = accs_by_id.get(acc_id)
        if not a:
            skipped_cooldown.append({"acc_id": acc_id, "reason": "acc not found"})
            continue
        cd = redis.get(_acc_cooldown_key(acc_id))
        if cd:
            try:
                expires = int(cd)
            except Exception:
                expires = now_ts + 3600
            remain = max(0, expires - now_ts)
            skipped_cooldown.append({"acc_id": acc_id, "phone": a.phone, "reason": f"cooldown {remain}с"})
            continue
        if not a.proxy_id:
            skipped_cooldown.append({"acc_id": acc_id, "phone": a.phone, "reason": "нет прокси"})
            continue
        active_accs.append(a)

    if not active_accs:
        payload = {
            "status": "error", "job_id": job_id,
            "error": "нет активных аккаунтов (все на cooldown или без прокси)",
            "skipped": skipped_cooldown,
            "ts": now_ts,
        }
        redis.setex(prog_k, 600, json.dumps(payload, ensure_ascii=False))
        return payload

    # ── Group by proxy_id ──
    proxy_groups: dict[int, list] = defaultdict(list)
    for a in active_accs:
        proxy_groups[a.proxy_id].append(a)

    # ── Concurrency: RANDOM 2..min(7, N_proxies) ──
    max_conc_hard = int(params.get("max_concurrency", 7))
    min_conc_hard = int(params.get("min_concurrency", 2))
    n_proxies = len(proxy_groups)
    lo = max(1, min(min_conc_hard, n_proxies))
    hi = max(lo, min(max_conc_hard, n_proxies))
    concurrency = random.randint(lo, hi) if hi > lo else lo
    logger.info(
        f"[parser.multi] job={job_id[:12]} accs={len(active_accs)} "
        f"proxies={n_proxies} concurrency={concurrency} (range {lo}..{hi})"
    )

    # ── Split keywords round-robin among active accs ──
    all_keywords = [k.strip() for k in params.get("keywords", "").split(",") if k.strip()]
    if not all_keywords:
        payload = {
            "status": "error", "job_id": job_id,
            "error": "нет ключевых слов",
            "ts": now_ts,
        }
        redis.setex(prog_k, 600, json.dumps(payload, ensure_ascii=False))
        return payload

    # each acc gets kw_list[i::N]. Random offset чтобы не всегда acc[0] брал первый.
    offset = random.randint(0, max(0, len(active_accs) - 1))
    kw_by_acc: dict[int, list[str]] = {}
    for i, a in enumerate(active_accs):
        idx = (i + offset) % len(active_accs)
        kw_by_acc[a.id] = all_keywords[idx::len(active_accs)]

    # ── Shared progress state ──
    state = {
        "status": "running",
        "job_id": job_id,
        "concurrency": concurrency,
        "total_accounts": len(active_accs),
        "started_at": now_ts,
        "found_total": 0,
        "saved_total": 0,
        "keywords_total": len(all_keywords),
        "skipped": skipped_cooldown,
        "accounts": {
            a.id: {
                "phone": a.phone,
                "proxy_id": a.proxy_id,
                "status": "queued",
                "current_kw": "",
                "found": 0, "saved": 0,
                "keywords_done": 0,
                "keywords_total": len(kw_by_acc[a.id]),
                "flood_wait": 0,
                "flood_events": 0,
            } for a in active_accs
        },
    }

    def _pub(extra: dict | None = None):
        state["ts"] = int(time.time())
        if extra:
            state.update(extra)
        try:
            redis.setex(prog_k, 3600, json.dumps(state, ensure_ascii=False))
        except Exception as e:
            logger.debug(f"[parser.multi] publish fail: {e}")

    _pub()

    # ── Per-account worker ──
    only_with_comments = bool(params.get("only_with_comments", True))
    min_subs = int(params.get("min_subscribers", 0))
    max_subs = int(params.get("max_subscribers", 10_000_000))
    active_hours = int(params.get("active_hours", 0))
    limit_per_kw = int(params.get("limit_per_keyword", 50))
    max_per_acc = int(params.get("max_channels_per_acc", 500))
    name_endings = [e.strip().lower() for e in (params.get("name_endings") or "").split(",") if e.strip()]
    name_contains = [p.strip().lower() for p in (params.get("name_contains") or "").split(",") if p.strip()]
    pause_kw_min = float(params.get("pause_between_keywords_min", 3.0))
    pause_kw_max = float(params.get("pause_between_keywords_max", 6.0))
    pause_ch_min = float(params.get("pause_between_channels_min", 0.8))
    pause_ch_max = float(params.get("pause_between_channels_max", 1.5))

    async def _sync_flood_cooldown(acc_id: int, seconds: int):
        """Уложить аккаунт на перекур на min 1 час (никаких retry)."""
        cd_ttl = max(3600, int(seconds) + 60)
        expires = int(time.time()) + cd_ttl
        try:
            redis.setex(_acc_cooldown_key(acc_id), cd_ttl, str(expires))
        except Exception:
            pass
        state["accounts"][acc_id]["status"] = "flooded"
        state["accounts"][acc_id]["flood_wait"] = int(seconds)
        state["accounts"][acc_id]["flood_expires_at"] = expires
        state["accounts"][acc_id]["flood_events"] += 1

    async def _process_account(a):
        from utils.account_lock import acquire_account_lock, release_account_lock
        acc_id = a.id
        proxy = proxies_by_id.get(a.proxy_id)
        client = None

        # ── Account lock: не берём акк если warmup / commenting / другая
        # задача им уже пользуется. Иначе получим две сессии Telethon на
        # одном session-файле → conflict + возможный бан. ────────────────
        # TTL 300с: если за 5 мин парсер не отпустит — форс-освобождается.
        # Между keyword'ами release+reacquire — это позволяет warmup/comment
        # перехватить акк во время нашей паузы.
        if not acquire_account_lock(acc_id, ttl=300):
            state["accounts"][acc_id]["status"] = "busy_other"
            state["accounts"][acc_id]["error"] = "занят другой задачей (warmup/commenting/refix)"
            _pub()
            logger.info(f"[parser.multi] acc={acc_id} busy — skip (warmup/comment holds lock)")
            return
        _lock_held = True

        # Внешний try/finally — гарантированный release лока даже если
        # ниже что-то упадёт на этапе connect.
        try:
            try:
                client = make_telethon_client(a, proxy)
                if not client:
                    state["accounts"][acc_id]["status"] = "no_session"
                    _pub()
                    return
                await client.connect()
                if not await client.is_user_authorized():
                    state["accounts"][acc_id]["status"] = "not_authorized"
                    _pub()
                    return
            except Exception as e:
                state["accounts"][acc_id]["status"] = "connect_fail"
                state["accounts"][acc_id]["error"] = str(e)[:200]
                _pub()
                return

            state["accounts"][acc_id]["status"] = "running"
            _pub()

            keywords = kw_by_acc.get(acc_id, [])
            seen_local = set()

            for kw_i, kw in enumerate(keywords):
                if redis.get(stop_k):
                    state["accounts"][acc_id]["status"] = "cancelled"
                    break
                if state["accounts"][acc_id]["found"] >= max_per_acc:
                    break

                # Cooperative yield: между keyword'ами refresh лока с коротким
                # TTL. Если warmup/commenting захотят акк — они не смогут пока
                # мы держим лок. Даём им шанс: раз в keyword ОТПУСКАЕМ на
                # 3-5 сек, потом пробуем reacquire. Если не удалось — уходим.
                if kw_i > 0:
                    release_account_lock(acc_id)
                    _lock_held = False
                    # Даём другой задаче шанс схватить (SETNX это увидит)
                    await asyncio.sleep(random.uniform(3, 6))
                    if not acquire_account_lock(acc_id, ttl=300):
                        # Кто-то перехватил (warmup/commenting) — уступаем
                        state["accounts"][acc_id]["status"] = "yielded"
                        state["accounts"][acc_id]["error"] = "передан warmup/commenting"
                        _pub()
                        logger.info(
                            f"[parser.multi] acc={acc_id} yielded to another task "
                            f"после kw {kw_i}/{len(keywords)}"
                        )
                        # Отключаемся — держать client без лока опасно
                        try: await client.disconnect()
                        except Exception: pass
                        return
                    _lock_held = True

                state["accounts"][acc_id]["current_kw"] = kw
                state["accounts"][acc_id]["keywords_done"] = kw_i
                _pub()

                try:
                    res = await client(TgSearchRequest(q=kw, limit=limit_per_kw))
                except FloodWaitError as e:
                    logger.warning(
                        f"[parser.multi] acc={acc_id} FLOOD on TgSearchRequest kw='{kw}' "
                        f"{e.seconds}s → cooldown, exit acc"
                    )
                    await _sync_flood_cooldown(acc_id, e.seconds)
                    await _log_event(
                        user_id, "flood_wait", source="search_multi",
                        account_id=acc_id, wait_seconds=e.seconds,
                        seed=kw, details="TgSearchRequest",
                    )
                    _pub()
                    return
                except Exception as e:
                    logger.warning(f"[parser.multi] acc={acc_id} TgSearchRequest '{kw}' fail: {e}")
                    continue

                for chat in res.chats:
                    if redis.get(stop_k):
                        break
                    if state["accounts"][acc_id]["found"] >= max_per_acc:
                        break
                    if not getattr(chat, "username", None):
                        continue
                    uname_l = chat.username.lower()
                    if uname_l in seen_local:
                        continue
                    seen_local.add(uname_l)

                    # username filters
                    if name_endings and not any(uname_l.endswith(e) for e in name_endings):
                        continue
                    if name_contains and not any(p in uname_l for p in name_contains):
                        continue
                    subs = getattr(chat, "participants_count", 0) or 0
                    if subs == 0 or subs < min_subs or subs > max_subs:
                        continue

                    # cross-account dedup (Redis SET). Атомарно резервируем username.
                    try:
                        added = redis.sadd(dedup_k, uname_l)
                        redis.expire(dedup_k, 3600)
                    except Exception:
                        added = 1
                    if not added:
                        continue

                    # PRECISE has_comments via GetFullChannel (always)
                    has_comments, fw = await _check_has_comments(client, chat)
                    if fw > 0:
                        logger.warning(
                            f"[parser.multi] acc={acc_id} FLOOD on GetFullChannel "
                            f"@{chat.username} {fw}s → cooldown, exit acc"
                        )
                        await _sync_flood_cooldown(acc_id, fw)
                        await _log_event(
                            user_id, "flood_wait", source="search_multi",
                            account_id=acc_id, wait_seconds=fw,
                            seed=chat.username, details="GetFullChannel",
                        )
                        # снимаем резерв — другой акк может проверить
                        try: redis.srem(dedup_k, uname_l)
                        except: pass
                        _pub()
                        return

                    if only_with_comments and not has_comments:
                        continue

                    last_post = None
                    if active_hours > 0:
                        last_post = await _get_last_post_date(client, chat)
                        if last_post:
                            cutoff = datetime.utcnow() - timedelta(hours=active_hours)
                            if last_post.replace(tzinfo=None) < cutoff:
                                continue

                    ch_data = {
                        "channel_id": chat.id,
                        "username": chat.username,
                        "title": chat.title,
                        "subscribers": subs,
                        "has_comments": has_comments,
                        "last_post_date": last_post.isoformat() if last_post else None,
                        "search_query": kw,
                    }
                    try:
                        was_new = await _save_channel(user_id, ch_data)
                    except Exception as e:
                        logger.debug(f"[parser.multi] save @{chat.username} fail: {e}")
                        was_new = False

                    state["accounts"][acc_id]["found"] += 1
                    state["found_total"] += 1
                    if was_new:
                        state["accounts"][acc_id]["saved"] += 1
                        state["saved_total"] += 1
                    _pub()

                    # inter-channel pause
                    if await _sleep_cancellable(
                        random.uniform(pause_ch_min, pause_ch_max), redis, stop_k
                    ):
                        break

                # inter-keyword pause
                if await _sleep_cancellable(
                    random.uniform(pause_kw_min, pause_kw_max), redis, stop_k
                ):
                    break

            state["accounts"][acc_id]["keywords_done"] = min(
                len(keywords), state["accounts"][acc_id]["keywords_done"] + 1
            )
            if state["accounts"][acc_id]["status"] not in ("flooded", "cancelled", "yielded"):
                state["accounts"][acc_id]["status"] = "done"
            _pub()
        finally:
            # Гарантированно освобождаем лок И disconnect — чтобы warmup/
            # commenting могли забрать акк даже если наверху что-то упало.
            try:
                if client is not None:
                    await client.disconnect()
            except Exception:
                pass
            if _lock_held:
                try: release_account_lock(acc_id)
                except Exception: pass

    # ── Per-proxy sequential worker (ходит по своей группе акков) ──
    async def _proxy_worker(proxy_id: int, accs: list):
        # STAGGER 0-30 сек — чтобы N воркеров не стартанули в 1 секунду
        stagger = random.uniform(0, 30)
        await asyncio.sleep(stagger)
        for a in accs:
            if redis.get(stop_k):
                return
            await _process_account(a)
            # inter-account within same proxy: pause 30-90 сек
            if await _sleep_cancellable(random.uniform(30, 90), redis, stop_k):
                return

    # ── Run all proxy_workers with Semaphore(concurrency) ──
    sem = asyncio.Semaphore(concurrency)

    async def _capped(pid, accs):
        async with sem:
            await _proxy_worker(pid, accs)

    tasks = [_capped(pid, accs) for pid, accs in proxy_groups.items()]
    await asyncio.gather(*tasks, return_exceptions=True)

    # ── Final ──
    if redis.get(stop_k):
        state["status"] = "cancelled"
    else:
        state["status"] = "done"
    _pub()

    # log session summary
    await _log_event(
        user_id, "session_done", source="search_multi", account_id=None,
        channels_found=state["found_total"],
        channels_saved=state["saved_total"],
        duration_sec=int(time.time() - state["started_at"]),
        details=f"accs={len(active_accs)} conc={concurrency} kw={len(all_keywords)} "
                f"flooded={sum(1 for x in state['accounts'].values() if x['status']=='flooded')}",
    )

    return {
        "job_id": job_id,
        "found": state["found_total"],
        "saved": state["saved_total"],
        "accounts": len(active_accs),
    }


# ═══════════════════════════════════════════════════════════
# Celery entrypoint
# ═══════════════════════════════════════════════════════════

@celery_app.task(
    bind=True,
    name="tasks.parser_multi_tasks.run_parser_multi_search",
    acks_late=False,
    reject_on_worker_lost=False,
)
def run_parser_multi_search(self, user_id: int, account_ids: list[int], params: dict):
    """Фоновый мульти-аккаунтный парсер каналов.

    Args:
        user_id: владелец
        account_ids: список TelegramAccount.id для использования
        params: SearchRequest-совместимый dict (keywords, min_subscribers, etc)
                + опциональные min_concurrency (default 2), max_concurrency (default 7)
                + job_id (если None → генерируем)
    """
    import redis as redis_lib
    r = redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    job_id_early = params.get("job_id") or ""
    # Legacy ключ (user-level) для backward-compat
    r.setex(_task_id_key(user_id), 7200, self.request.id)
    # Per-job ключ — нужен для force-reset (revoke terminate=True) конкретного job'а
    if job_id_early:
        r.setex(_task_id_key(user_id, job_id_early), 7200, self.request.id)
    return _run_async(_run_multi(user_id, account_ids, params))
