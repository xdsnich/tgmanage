"""
GramGPT — tasks/parser_similar_multi_tasks.py

Мульти-аккаунтная ПАУТИНКА (similar crawler).

Отличия от parser_similar_tasks.run_similar_crawler:
  1. Работает с N аккаунтов сразу.
  2. Случайное число одновременных воркеров: random.randint(2, min(7, N_proxies)).
  3. Один прокси = один worker (аккаунты за общим прокси идут ПОСЛЕДОВАТЕЛЬНО).
  4. Один connect() на весь run аккаунта.
  5. FloodWait → БЕЗ retry. Redis-cooldown `parser:acc_cooldown:{acc_id}`
     на max(3600, fw+60). Аккаунт выходит из run, warmup/commenting не будет
     трогать его пока cooldown жив.
  6. account_lock: перед `_process_account` пытаемся acquire, если busy —
     skip акк (warmup/commenting/refix уже держат).
  7. Cooperative yield: между seed'ами release + reacquire, если кто-то
     схватил — уступаем.
  8. Общий Redis SET `seen` — все аккаунты видят одну и ту же карту дедупа,
     не парсят одинаковые каналы параллельно.
  9. Split seeds round-robin среди активных акков. Каждый акк получает
     свой уникальный набор seeds → минимум пересечений.

Progress (`parser:similar_multi:progress:{user_id}:{job_id}`):
    {
      "status": "running" | "done" | "cancelled" | "error",
      "job_id": ...,
      "concurrency": 3,
      "total_accounts": 10,
      "started_at": <ts>,
      "found_total": 47,
      "saved_total": 32,
      "queue_size": 12,           # общий размер очереди по всем аккам
      "accounts": {
        123: {"phone":"+X", "status":"running", "current_seed":"@crypto",
              "depth":1, "found":5, "saved":3, "seeds_done":2, "seeds_total":10,
              "flood_wait":0, ...},
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
from collections import defaultdict, deque
from datetime import datetime

from celery_app import celery_app

logger = logging.getLogger(__name__)
API_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)


# ═══════════════════════════════════════════════════════════
# Redis keys
# ═══════════════════════════════════════════════════════════

def _progress_key(user_id: int, job_id: str) -> str:
    return f"parser:similar_multi:progress:{user_id}:{job_id}"


def _stop_key(user_id: int, job_id: str) -> str:
    return f"parser:similar_multi:stop:{user_id}:{job_id}"


def _task_id_key(user_id: int, job_id: str = "") -> str:
    if job_id:
        return f"parser:similar_multi:task_id:{user_id}:{job_id}"
    return f"parser:similar_multi:task_id:{user_id}"


def _acc_cooldown_key(acc_id: int) -> str:
    # Общий с обычным парсером — уважают все парсеры
    return f"parser:acc_cooldown:{acc_id}"


def _seen_key(user_id: int, job_id: str) -> str:
    """Redis SET username'ов увиденных этим job'ом (BFS-дедуп).
    Кросс-аккаунтный — все воркеры видят одинаковую картину."""
    return f"parser:similar_multi:seen:{user_id}:{job_id}"


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
# Event logging + DB
# ═══════════════════════════════════════════════════════════

async def _log_event(user_id, event_type, **kwargs):
    try:
        from utils.parser_events import log_event
        await log_event(user_id=user_id, event_type=event_type, **kwargs)
    except Exception as e:
        logger.debug(f"[parser_events] skip: {e}")


async def _save_channel(user_id: int, channel_data: dict) -> bool:
    """UPSERT-ish: если username уже есть у юзера — не сохраняем. True = новый."""
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
        row = {
            "user_id": user_id,
            "channel_id": channel_data.get("channel_id", 0),
            "username": channel_data["username"],
            "title": channel_data.get("title", ""),
            "subscribers": channel_data.get("subscribers", 0),
            "has_comments": channel_data.get("has_comments", False),
            "search_query": channel_data.get("search_query", ""),
            "folder": channel_data.get("folder", ""),
        }
        if hasattr(ParsedChannel, "source"):
            row["source"] = "similar_multi"
        db.add(ParsedChannel(**row))
        await db.commit()
        return True


# ═══════════════════════════════════════════════════════════
# Telethon helpers
# ═══════════════════════════════════════════════════════════

async def _get_recommendations(client, channel_entity):
    from telethon.tl.functions.channels import GetChannelRecommendationsRequest
    from telethon.errors import FloodWaitError
    try:
        result = await client(GetChannelRecommendationsRequest(channel=channel_entity))
        return ("ok", getattr(result, "chats", []))
    except FloodWaitError as e:
        return ("flood", e.seconds)
    except Exception as e:
        return ("err", str(e)[:150])


async def _check_has_comments(client, chat):
    """(commentable, flood_wait_seconds). Precise-check через GetFullChannel.

    ── 2026-08-22 ───────────────────────────────────────────────
    Мало того что у канала есть discussion-чат — туда ещё должно быть
    можно писать. Из того же ответа берём join_request (вступление по
    заявке) и default_banned_rights (запрет писать) и отсеиваем такие
    каналы прямо на паутинке. Дополнительных запросов ноль.
    """
    from utils.channel_caps import check_and_record, entity_username
    # entity_username учитывает мульти-юзернеймы: у части каналов
    # поле username пустое, а имя лежит в списке usernames.
    uname = entity_username(chat)
    ok, fw, caps = await check_and_record(client, chat, uname, source="parser_crawler")
    if not ok and caps.get("reason") and caps.get("reason") != "no_discussion":
        logger.info(f"[crawler.multi] @{uname}: отсеян — {caps['reason']}")
    return (ok, fw)


async def _get_entity_safe(client, username):
    from telethon.errors import FloodWaitError, UsernameNotOccupiedError, ChannelPrivateError
    try:
        return ("ok", await client.get_entity(username))
    except FloodWaitError as e:
        return ("flood", e.seconds)
    except (UsernameNotOccupiedError, ChannelPrivateError):
        return ("skip", None)
    except Exception as e:
        return ("err", str(e)[:120])


# ═══════════════════════════════════════════════════════════
# Основной multi-паутинка
# ═══════════════════════════════════════════════════════════

async def _run_similar_multi(user_id: int, account_ids: list[int], params: dict):
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload
    from models.account import TelegramAccount
    from models.proxy import Proxy
    from utils.telegram import make_telethon_client
    from utils.db_pool import async_session as Session

    import redis as redis_lib
    redis = redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    job_id = params.get("job_id") or f"similar_multi_{int(time.time())}_{random.randint(1000,9999)}"
    prog_k = _progress_key(user_id, job_id)
    stop_k = _stop_key(user_id, job_id)
    seen_k = _seen_key(user_id, job_id)
    redis.delete(stop_k)

    # ── Load accounts + proxies ──
    accs_by_id: dict[int, "TelegramAccount"] = {}
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
        proxy_ids = list({a.proxy_id for a in accs_by_id.values() if a.proxy_id})
        if proxy_ids:
            pr = await db.execute(select(Proxy).where(Proxy.id.in_(proxy_ids)))
            for p in pr.scalars().all():
                proxies_by_id[p.id] = p

    # ── Отсеиваем cooldown + no-proxy ──
    now_ts = int(time.time())
    skipped: list[dict] = []
    active_accs = []
    for acc_id in account_ids:
        a = accs_by_id.get(acc_id)
        if not a:
            skipped.append({"acc_id": acc_id, "reason": "acc not found"})
            continue
        cd = redis.get(_acc_cooldown_key(acc_id))
        if cd:
            try: expires = int(cd)
            except Exception: expires = now_ts + 3600
            remain = max(0, expires - now_ts)
            skipped.append({"acc_id": acc_id, "phone": a.phone, "reason": f"cooldown {remain}с"})
            continue
        if not a.proxy_id:
            skipped.append({"acc_id": acc_id, "phone": a.phone, "reason": "нет прокси"})
            continue
        active_accs.append(a)

    if not active_accs:
        payload = {
            "status": "error", "job_id": job_id,
            "error": "нет активных аккаунтов (все на cooldown или без прокси)",
            "skipped": skipped, "ts": now_ts,
        }
        redis.setex(prog_k, 600, json.dumps(payload, ensure_ascii=False))
        return payload

    # ── Group by proxy ──
    proxy_groups: dict[int, list] = defaultdict(list)
    for a in active_accs:
        proxy_groups[a.proxy_id].append(a)

    # ── Concurrency ──
    max_conc = int(params.get("max_concurrency", 7))
    min_conc = int(params.get("min_concurrency", 2))
    n_proxies = len(proxy_groups)
    lo = max(1, min(min_conc, n_proxies))
    hi = max(lo, min(max_conc, n_proxies))
    concurrency = random.randint(lo, hi) if hi > lo else lo
    logger.info(
        f"[similar.multi] job={job_id[:12]} accs={len(active_accs)} "
        f"proxies={n_proxies} concurrency={concurrency}"
    )

    # ── Params ──
    seeds_raw = params.get("seeds") or []
    seeds = [s.strip().lstrip("@") for s in seeds_raw if s and s.strip()]
    if not seeds:
        payload = {"status": "error", "job_id": job_id, "error": "нет seed-каналов", "ts": now_ts}
        redis.setex(prog_k, 600, json.dumps(payload, ensure_ascii=False))
        return payload

    max_depth = int(params.get("max_depth", 2))
    max_channels = int(params.get("max_channels", 1000))
    folder = params.get("folder", "")
    pause_min = float(params.get("pause_min", 8.0))
    pause_max = float(params.get("pause_max", 15.0))
    # precise_check ВСЕГДА, чтобы сохранять сразу правильный has_comments.
    # only_with_comments — фильтр: сохраняем/уходим глубже только по каналам
    # у которых есть discussion-группа (кому есть куда комментить).
    only_with_comments = bool(params.get("only_with_comments", True))
    # Пауза перед каждым GetFullChannel — не бомбим TG сотнями запросов.
    check_pause_min = float(params.get("check_pause_min", 0.6))
    check_pause_max = float(params.get("check_pause_max", 1.4))

    # ── Инициализируем общий seen SET seeds'ами ──
    try:
        if seeds:
            redis.sadd(seen_k, *seeds)
            redis.expire(seen_k, 24 * 3600)
    except Exception as e:
        logger.warning(f"[similar.multi] seen init fail: {e}")

    # ── Split seeds round-robin среди активных акков ──
    offset = random.randint(0, max(0, len(active_accs) - 1))
    seeds_by_acc: dict[int, list[str]] = {}
    for i, a in enumerate(active_accs):
        idx = (i + offset) % len(active_accs)
        seeds_by_acc[a.id] = seeds[idx::len(active_accs)]

    # ── Shared state ──
    state = {
        "status": "running",
        "job_id": job_id,
        "concurrency": concurrency,
        "total_accounts": len(active_accs),
        "started_at": now_ts,
        "found_total": 0,
        "saved_total": 0,
        "max_channels": max_channels,
        "max_depth": max_depth,
        "seeds_total": len(seeds),
        "skipped": skipped,
        "accounts": {
            a.id: {
                "phone": a.phone,
                "proxy_id": a.proxy_id,
                "status": "queued",
                "current_seed": "",
                "depth": 0,
                "found": 0, "saved": 0,
                "seeds_done": 0,
                "seeds_total": len(seeds_by_acc[a.id]),
                "queue_size": 0,
                "flood_wait": 0,
                "flood_events": 0,
            } for a in active_accs
        },
    }

    def _pub(extra: dict | None = None):
        state["ts"] = int(time.time())
        # Общий queue_size — сумма по всем акам
        state["queue_size"] = sum(x.get("queue_size", 0) for x in state["accounts"].values())
        if extra:
            state.update(extra)
        try:
            redis.setex(prog_k, 3600, json.dumps(state, ensure_ascii=False))
        except Exception as e:
            logger.debug(f"[similar.multi] publish fail: {e}")

    _pub()

    async def _sync_flood_cooldown(acc_id: int, seconds: int):
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

    # ── Per-account BFS worker ──
    async def _process_account(a):
        from utils.account_lock import acquire_account_lock, release_account_lock
        acc_id = a.id
        proxy = proxies_by_id.get(a.proxy_id)
        client = None

        # account_lock guard — не берём если warmup/commenting/refix уже держат
        if not acquire_account_lock(acc_id, ttl=300):
            state["accounts"][acc_id]["status"] = "busy_other"
            state["accounts"][acc_id]["error"] = "занят другой задачей (warmup/commenting/refix)"
            _pub()
            return
        _lock_held = True

        try:
            try:
                client = make_telethon_client(a, proxy)
                if not client:
                    state["accounts"][acc_id]["status"] = "no_session"
                    _pub(); return
                await client.connect()
                if not await client.is_user_authorized():
                    state["accounts"][acc_id]["status"] = "not_authorized"
                    _pub(); return
            except Exception as e:
                state["accounts"][acc_id]["status"] = "connect_fail"
                state["accounts"][acc_id]["error"] = str(e)[:200]
                _pub(); return

            state["accounts"][acc_id]["status"] = "running"
            _pub()

            # Локальная BFS-очередь для этого акка
            my_seeds = seeds_by_acc.get(acc_id, [])
            queue: deque = deque([(s, 0) for s in my_seeds])
            origin_map: dict[str, str] = {s: s for s in my_seeds}
            state["accounts"][acc_id]["queue_size"] = len(queue)

            visited_by_this_acc = 0

            while queue:
                if redis.get(stop_k):
                    state["accounts"][acc_id]["status"] = "cancelled"
                    break
                if state["found_total"] >= max_channels:
                    break

                # Cooperative yield — release/reacquire между визитами
                if visited_by_this_acc > 0:
                    release_account_lock(acc_id)
                    _lock_held = False
                    await asyncio.sleep(random.uniform(3, 6))
                    if not acquire_account_lock(acc_id, ttl=300):
                        state["accounts"][acc_id]["status"] = "yielded"
                        state["accounts"][acc_id]["error"] = "передан warmup/commenting"
                        _pub()
                        try: await client.disconnect()
                        except Exception: pass
                        return
                    _lock_held = True

                username, depth = queue.popleft()
                state["accounts"][acc_id]["queue_size"] = len(queue)
                if depth >= max_depth:
                    continue

                root_seed = origin_map.get(username, username)
                state["accounts"][acc_id]["current_seed"] = username
                state["accounts"][acc_id]["depth"] = depth
                _pub()

                # get_entity
                ent_status, ent_val = await _get_entity_safe(client, username)
                if ent_status == "flood":
                    await _sync_flood_cooldown(acc_id, ent_val)
                    await _log_event(
                        user_id, "flood_wait", source="similar_multi",
                        account_id=acc_id, wait_seconds=ent_val,
                        seed=username, details="get_entity",
                    )
                    _pub()
                    try: await client.disconnect()
                    except Exception: pass
                    return
                if ent_status != "ok" or ent_val is None:
                    continue
                entity = ent_val

                # pause между визитами
                if await _sleep_cancellable(random.uniform(pause_min, pause_max), redis, stop_k):
                    break

                # recommendations
                rec_status, rec_val = await _get_recommendations(client, entity)
                if rec_status == "flood":
                    await _sync_flood_cooldown(acc_id, rec_val)
                    await _log_event(
                        user_id, "flood_wait", source="similar_multi",
                        account_id=acc_id, wait_seconds=rec_val,
                        seed=username, details="recommendations",
                    )
                    _pub()
                    try: await client.disconnect()
                    except Exception: pass
                    return
                if rec_status != "ok":
                    continue
                similar_chats = rec_val
                state["accounts"][acc_id]["seeds_done"] += 1
                visited_by_this_acc += 1

                for chat in similar_chats:
                    if state["found_total"] >= max_channels:
                        break
                    if redis.get(stop_k):
                        break
                    if not getattr(chat, "username", None):
                        continue
                    uname = chat.username

                    # Кросс-акк дедуп через Redis SET
                    try:
                        added = redis.sadd(seen_k, uname)
                    except Exception:
                        added = 1
                    if not added:
                        continue

                    subs = getattr(chat, "participants_count", 0) or 0
                    title = getattr(chat, "title", uname) or uname

                    # ── Precise-check has_comments (GetFullChannel) ──
                    # Это +1 запрос TG per candidate, но сохраняем сразу
                    # правильный флаг → не нужно потом отдельно верифить.
                    has_c, fw = await _check_has_comments(client, chat)
                    if fw > 0:
                        # FloodWait на GetFullChannel → cooldown ≥1ч, exit acc.
                        # Убираем этот username из seen чтобы другой акк
                        # мог попробовать позже.
                        try: redis.srem(seen_k, uname)
                        except Exception: pass
                        await _sync_flood_cooldown(acc_id, fw)
                        await _log_event(
                            user_id, "flood_wait", source="similar_multi",
                            account_id=acc_id, wait_seconds=fw,
                            seed=uname, details="GetFullChannel",
                        )
                        _pub()
                        try: await client.disconnect()
                        except Exception: pass
                        return

                    # ── Фильтр результата ≠ фильтр обхода (2026-08-22) ──
                    # Раньше непригодный канал обрывал ветку целиком: его не
                    # сохраняли И не шли через него вглубь. Но пригодность для
                    # КОММЕНТИРОВАНИЯ и ценность как ИСТОЧНИКА СОСЕДЕЙ — разные
                    # вещи. Крупный канал без обсуждений или с вступлением по
                    # заявке отлично ведёт к тематическим соседям, в которых
                    # писать можно.
                    #
                    # Теперь: в результаты не сохраняем, но по ссылкам проходим.
                    # Так фильтрация не сужает охват паутинки.
                    _skip_save = bool(only_with_comments and not has_c)
                    if _skip_save:
                        if depth + 1 < max_depth:
                            origin_map[uname] = root_seed
                            queue.append((uname, depth + 1))
                            state["accounts"][acc_id]["queue_size"] = len(queue)
                        # Мини-пауза чтобы не бомбить TG сразу следующим get_full
                        await asyncio.sleep(random.uniform(check_pause_min, check_pause_max))
                        continue

                    ch_data = {
                        "channel_id": chat.id,
                        "username": uname,
                        "title": title,
                        "subscribers": subs,
                        "has_comments": has_c,
                        "search_query": f"similar:@{root_seed}",
                        "folder": folder,
                    }
                    try:
                        is_new = await _save_channel(user_id, ch_data)
                    except Exception as e:
                        logger.debug(f"[similar.multi] save @{uname} fail: {e}")
                        is_new = False

                    state["accounts"][acc_id]["found"] += 1
                    state["found_total"] += 1
                    if is_new:
                        state["accounts"][acc_id]["saved"] += 1
                        state["saved_total"] += 1

                    # Уходим глубже только по каналам с комментами
                    # (или если фильтр выключен — по всем)
                    if depth + 1 < max_depth:
                        origin_map[uname] = root_seed
                        queue.append((uname, depth + 1))
                        state["accounts"][acc_id]["queue_size"] = len(queue)

                    # Мини-пауза между GetFullChannel-ями
                    await asyncio.sleep(random.uniform(check_pause_min, check_pause_max))

                _pub()

            # Финал по этому акку
            if state["accounts"][acc_id]["status"] not in ("flooded", "cancelled", "yielded"):
                state["accounts"][acc_id]["status"] = "done"
            _pub()

        finally:
            try:
                if client is not None:
                    await client.disconnect()
            except Exception:
                pass
            if _lock_held:
                try: release_account_lock(acc_id)
                except Exception: pass

    # ── Per-proxy sequential worker ──
    async def _proxy_worker(proxy_id: int, accs: list):
        stagger = random.uniform(0, 30)
        await asyncio.sleep(stagger)
        for a in accs:
            if redis.get(stop_k):
                return
            if state["found_total"] >= max_channels:
                return
            await _process_account(a)
            if await _sleep_cancellable(random.uniform(30, 90), redis, stop_k):
                return

    # ── Run ──
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

    await _log_event(
        user_id, "session_done", source="similar_multi", account_id=None,
        channels_found=state["found_total"],
        channels_saved=state["saved_total"],
        duration_sec=int(time.time() - state["started_at"]),
        details=(
            f"accs={len(active_accs)} conc={concurrency} seeds={len(seeds)} "
            f"max_depth={max_depth} max_channels={max_channels} "
            f"flooded={sum(1 for x in state['accounts'].values() if x['status']=='flooded')}"
        ),
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
    name="tasks.parser_similar_multi_tasks.run_similar_multi_crawler",
    acks_late=False,
    reject_on_worker_lost=False,
)
def run_similar_multi_crawler(self, user_id: int, account_ids: list[int], params: dict):
    """Multi-акк паутинка. See module docstring."""
    import redis as redis_lib
    r = redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    job_id_early = params.get("job_id") or ""
    r.setex(_task_id_key(user_id), 7200, self.request.id)
    if job_id_early:
        r.setex(_task_id_key(user_id, job_id_early), 7200, self.request.id)
    return _run_async(_run_similar_multi(user_id, account_ids, params))
