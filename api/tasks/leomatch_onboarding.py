"""GramGPT — tasks/leomatch_onboarding.py
Celery-worker для массовой регистрации акков в @leomatchbot.

Один task = один batch (LeomatchOnboardingTask). Внутри — параллельно
с ограничением concurrency + per-IP throttle + jitter между стартами
(чтобы 2 акка с одним прокси не подключались одновременно, но и не
ждали друг друга последовательно).
"""

import asyncio
import random
import logging
import os
import sys
from datetime import datetime

API_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)

from celery_app import celery_app

logger = logging.getLogger(__name__)

# ── Параметры распараллеливания ──────────────────────────────
# Одновременно работающих акков (каждый — со своим прокси).
# Scheduler ГАРАНТИРУЕТ что 2 акка с одним прокси не идут параллельно
# (проверяет proxy_id перед запуском). Т.о. 4 = 4 разных прокси активны.
MAX_CONCURRENT = 4

# Задержка между стартами (после scheduler'а выбрал next кандидата).
# Даёт TG видеть размазанные подключения даже когда все прокси разные.
START_JITTER_MIN = 2
START_JITTER_MAX = 5

# IP-lock как страховка (вдруг warmup/comment task занимает тот же прокси)
IP_WAIT_POLL_MIN = 5
IP_WAIT_POLL_MAX = 12
IP_WAIT_TIMEOUT = 15 * 60


@celery_app.task(
    name="tasks.leomatch_onboarding.execute_batch",
    time_limit=3600,          # hard kill через 60 мин (для больших batch'ей)
    soft_time_limit=3300,     # soft warning на 55-й мин
)
def execute_batch(task_id: int):
    """Celery entry. Гоняет весь batch — parallel по items с IP-lock."""
    return asyncio.run(_execute_batch(task_id))


async def _execute_batch(task_id: int):
    """Greedy scheduler: до MAX_CONCURRENT одновременно, но НЕ два акка
    на одном прокси. По завершении item — сигнал scheduler'у попробовать
    запустить следующего с любым свободным прокси."""
    from sqlalchemy import select
    from utils.db_pool import async_session as Session
    from models.leomatch_onboarding import LeomatchOnboardingTask, LeomatchOnboardingItem
    from models.account import TelegramAccount

    async with Session() as db:
        task = (await db.execute(select(LeomatchOnboardingTask).where(LeomatchOnboardingTask.id == task_id))).scalar_one_or_none()
        if not task:
            logger.error(f"[leomatch] task #{task_id} not found")
            return {"status": "task_not_found"}

        task.status = "running"
        task.started_at = datetime.utcnow()
        task.error = ""
        await db.commit()

        # Одним запросом собираем (item_id, acc_id, proxy_id) для scheduling
        rows = (await db.execute(
            select(LeomatchOnboardingItem.id, LeomatchOnboardingItem.account_id, TelegramAccount.proxy_id)
            .join(TelegramAccount, TelegramAccount.id == LeomatchOnboardingItem.account_id)
            .where(
                LeomatchOnboardingItem.task_id == task_id,
                LeomatchOnboardingItem.status.in_(["pending", "error"]),
            )
            .order_by(LeomatchOnboardingItem.id.asc())
        )).all()

    if not rows:
        await _mark_finished(task_id)
        return {"status": "done", "task_id": task_id, "items": 0}

    logger.info(
        f"[leomatch] task #{task_id}: {len(rows)} items, "
        f"unique proxies={len(set(r[2] for r in rows if r[2]))}, "
        f"concurrency={MAX_CONCURRENT}"
    )

    # ── Scheduler state ─────────────────────────────────────
    remaining: list[tuple[int, int, int | None]] = list(rows)  # (item_id, acc_id, proxy_id)
    busy_proxies: set[int] = set()
    in_flight = 0
    release_event = asyncio.Event()
    running_tasks: list[asyncio.Task] = []

    def _pick_next() -> tuple[int, int | None] | None:
        """Первый item в очереди, чей proxy_id не в busy_proxies.
        None если ни один не подходит (все проксики заняты) или очередь пуста."""
        for i, (iid, acc_id, pid) in enumerate(remaining):
            if pid is None or pid not in busy_proxies:
                return i, pid
        return None

    async def _wrap(item_id: int, proxy_id: int | None):
        nonlocal in_flight
        try:
            await _process_item(item_id)
        finally:
            if proxy_id is not None:
                busy_proxies.discard(proxy_id)
            in_flight -= 1
            release_event.set()

    check_alive_counter = 0
    while remaining or in_flight > 0:
        # Каждые 5 итераций проверяем — не удалили ли task через UI
        check_alive_counter += 1
        if check_alive_counter >= 5:
            check_alive_counter = 0
            async with Session() as _db:
                _still = (await _db.execute(
                    select(LeomatchOnboardingTask.id).where(LeomatchOnboardingTask.id == task_id)
                )).scalar_one_or_none()
                if not _still:
                    logger.info(f"[leomatch] task #{task_id} DELETED — scheduler останавливается ({len(remaining)} items не запущено)")
                    remaining.clear()
                    # Ждём завершения in-flight и выходим
                    break

        picked = None
        if in_flight < MAX_CONCURRENT:
            picked = _pick_next()

        if picked is not None:
            idx, proxy_id = picked
            item_id, acc_id, pid = remaining.pop(idx)
            if pid is not None:
                busy_proxies.add(pid)
            in_flight += 1
            running_tasks.append(asyncio.create_task(_wrap(item_id, pid)))
            logger.info(f"[leomatch] launch item#{item_id} acc#{acc_id} proxy#{pid} (in_flight={in_flight}, remaining={len(remaining)})")
            # мелкий stagger между запусками
            await asyncio.sleep(random.uniform(START_JITTER_MIN, START_JITTER_MAX))
            continue

        # Либо concurrency full, либо все свободные прокси заняты — ждём event
        release_event.clear()
        try:
            await asyncio.wait_for(release_event.wait(), timeout=60)
        except asyncio.TimeoutError:
            # раз в минуту просыпаемся на всякий случай (не должно случаться)
            pass

    await asyncio.gather(*running_tasks, return_exceptions=True)
    await _mark_finished(task_id)
    return {"status": "done", "task_id": task_id, "items": len(rows)}


async def _process_item(item_id: int):
    """Один аккаунт: получить из БД → IP-lock (страховка от других tasks) → онбординг.

    Concurrency и «один прокси = один акк» гарантирует scheduler в _execute_batch."""
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload
    from utils.db_pool import async_session as Session
    from models.leomatch_onboarding import LeomatchOnboardingItem
    from models.account import TelegramAccount
    from models.proxy import Proxy
    from utils.telegram import make_telethon_client
    from utils.account_lock import acquire_account_lock, release_account_lock
    from utils.leomatch_bot import onboard_account

    async with Session() as db:
        item = (await db.execute(
            select(LeomatchOnboardingItem).where(LeomatchOnboardingItem.id == item_id)
        )).scalar_one_or_none()
        if not item:
            # task/item был удалён пока мы ждали в scheduler'е — тихо выходим
            logger.info(f"[leomatch] item #{item_id} исчез (task deleted) — skip")
            return

        acc = (await db.execute(
            select(TelegramAccount)
            .options(joinedload(TelegramAccount.api_app))
            .where(TelegramAccount.id == item.account_id)
        )).scalar_one_or_none()
        if not acc:
            item.status = "error"; item.error = "account not found"
            await db.commit(); return

        acc_status = acc.status.value if hasattr(acc.status, "value") else str(acc.status)
        if acc_status != "active":
            item.status = "error"; item.error = f"account status={acc_status}"
            await db.commit(); return

        proxy = (await db.execute(
            select(Proxy).where(Proxy.id == acc.proxy_id)
        )).scalar_one_or_none() if acc.proxy_id else None

        # ── Per-account lock (не даст warmup/comment task тем же аккам работать)
        if not acquire_account_lock(acc.id, ttl=600):
            item.status = "error"; item.error = "account locked (busy in other task)"
            await db.commit(); return

        # Сразу помечаем running — чтобы UI видел прогресс (не ждать connect).
        # Scheduler в _execute_batch уже гарантирует, что 2 акка с одним прокси
        # не запускаются параллельно, поэтому IP-lock здесь избыточен.
        item.status = "running"
        item.started_at = datetime.utcnow()
        await db.commit()

        client = None
        try:
            client = make_telethon_client(acc, proxy_row=proxy)
            if client is None:
                raise RuntimeError("make_telethon_client returned None (no session/proxy)")
            await client.connect()
            if not await client.is_user_authorized():
                raise RuntimeError("not authorized (session expired)")

            profile = _build_profile_from_json(item.profile)
            result = await onboard_account(client, profile)

            item.steps_log = result.get("log", [])
            if result.get("success"):
                item.status = "done"; item.error = ""
            else:
                item.status = "error"; item.error = (result.get("error") or "unknown")[:500]
            item.finished_at = datetime.utcnow()
            await db.commit()
            logger.info(f"[leomatch] item #{item_id} acc#{acc.id}: {item.status}")
        except Exception as e:
            logger.exception(f"[leomatch] item #{item_id} fatal")
            item.status = "error"
            item.error = f"{type(e).__name__}: {str(e)[:400]}"
            item.finished_at = datetime.utcnow()
            await db.commit()
        finally:
            try:
                if client is not None:
                    await client.disconnect()
            except Exception:
                pass
            release_account_lock(acc.id)


# ═══════════════════════════════════════════════════════════
# LIKES — для каждого done акка отправить random(min..max) лайков
# в @leomatchbot. Consecutive: 2 parallel + proxy-affinity + stagger.
# ═══════════════════════════════════════════════════════════

@celery_app.task(
    name="tasks.leomatch_onboarding.execute_likes_batch",
    time_limit=7200,
    soft_time_limit=7000,
)
def execute_likes_batch(task_id: int, likes_min: int = 5, likes_max: int = 11, max_parallel: int = 3):
    return asyncio.run(_execute_likes_batch(task_id, likes_min, likes_max, max_parallel))


LIKES_STATUS_KEY = "gramgpt:leomatch:likes:{task_id}"
LIKES_STOP_KEY = "gramgpt:leomatch:likes:{task_id}:stop"


def _likes_status_write(task_id: int, data: dict):
    """Пишем status в Redis (persistent, для UI progress)."""
    try:
        from utils.redis_pool import get_redis
        import json
        get_redis().setex(LIKES_STATUS_KEY.format(task_id=task_id), 3600, json.dumps(data))
    except Exception:
        pass


def _likes_stop_requested(task_id: int) -> bool:
    try:
        from utils.redis_pool import get_redis
        return bool(get_redis().get(LIKES_STOP_KEY.format(task_id=task_id)))
    except Exception:
        return False


async def _execute_likes_batch(task_id: int, likes_min: int, likes_max: int, max_parallel: int):
    from sqlalchemy import select, update as sa_update
    from utils.db_pool import async_session as Session
    from models.leomatch_onboarding import LeomatchOnboardingTask, LeomatchOnboardingItem
    from models.account import TelegramAccount

    MAX_PARALLEL = max(2, min(4, int(max_parallel)))   # clamp 2..4
    STAGGER = (5, 12)

    # Сбрасываем stop-flag на всякий случай (если раньше был set)
    try:
        from utils.redis_pool import get_redis
        get_redis().delete(LIKES_STOP_KEY.format(task_id=task_id))
    except Exception:
        pass

    async with Session() as db:
        rows = (await db.execute(
            select(LeomatchOnboardingItem.id, LeomatchOnboardingItem.account_id,
                   TelegramAccount.proxy_id)
            .join(TelegramAccount, TelegramAccount.id == LeomatchOnboardingItem.account_id)
            .where(
                LeomatchOnboardingItem.task_id == task_id,
                LeomatchOnboardingItem.status == "done",
            )
            .order_by(LeomatchOnboardingItem.id.asc())
        )).all()

    if not rows:
        return {"status": "no_done_items", "task_id": task_id}

    # Рандомизируем порядок акков (не всегда одинаково)
    rows = list(rows)
    random.shuffle(rows)

    logger.info(
        f"[leomatch/likes] task #{task_id}: {len(rows)} accounts, "
        f"likes {likes_min}-{likes_max} random each, "
        f"unique proxies={len(set(r[2] for r in rows if r[2]))}, "
        f"concurrency={MAX_PARALLEL}"
    )

    remaining = [(r[0], r[1], r[2]) for r in rows]
    busy = set()
    in_flight = 0
    release = asyncio.Event()
    tasks_list = []
    stats = {
        "accounts_done": 0, "total_likes": 0, "total_dislikes": 0, "total_matches": 0, "fail": 0,
        "accounts_total": len(rows), "in_flight": 0, "remaining": len(remaining),
        "status": "running", "started_at": datetime.utcnow().isoformat() + "Z",
        "likes_min": likes_min, "likes_max": likes_max, "max_parallel": MAX_PARALLEL,
        "errs": [], "last_events": [],
    }
    _likes_status_write(task_id, stats)

    def _sync_write():
        stats["in_flight"] = in_flight
        stats["remaining"] = len(remaining)
        _likes_status_write(task_id, stats)

    def _pick():
        for i, (iid, aid, pid) in enumerate(remaining):
            if pid is None or pid not in busy:
                return i, pid
        return None

    def _stop_check():
        return _likes_stop_requested(task_id)

    async def _wrap(iid, aid, pid):
        nonlocal in_flight
        try:
            n = random.randint(likes_min, likes_max)
            r = await _likes_one(iid, aid, n, _stop_check)
            stats["accounts_done"] += 1
            stats["total_likes"] += r["sent"]
            stats["total_dislikes"] += r.get("dislikes", 0)
            stats["total_matches"] += r["matches"]
            evt = f"acc#{aid}: likes={r['sent']} dislikes={r.get('dislikes',0)} matches={r['matches']}"
            if r.get("error") and r["error"] != "no_more_profiles":
                stats["fail"] += 1
                stats["errs"].append(f"acc#{aid}: {r['error'][:120]}")
                evt += f" ERR: {r['error'][:80]}"
            stats["last_events"].append(evt)
            stats["last_events"] = stats["last_events"][-15:]   # только последние 15
            logger.info(f"[leomatch/likes] {evt}")
            _sync_write()
        finally:
            if pid is not None:
                busy.discard(pid)
            in_flight -= 1
            release.set()

    while remaining or in_flight > 0:
        # Force-stop проверка перед каждой итерацией scheduler'а
        if _likes_stop_requested(task_id):
            logger.warning(f"[leomatch/likes] task #{task_id}: STOP requested → cancel remaining")
            stats["status"] = "stopping"
            _sync_write()
            remaining.clear()   # больше не запускаем новых
            # Ждём завершения in-flight и выходим
            if in_flight > 0:
                try:
                    await asyncio.wait_for(release.wait(), timeout=60)
                except asyncio.TimeoutError:
                    pass
            break
        picked = None
        if in_flight < MAX_PARALLEL:
            picked = _pick()
        if picked is not None:
            idx, pid = picked
            iid, aid, p = remaining.pop(idx)
            if p is not None:
                busy.add(p)
            in_flight += 1
            tasks_list.append(asyncio.create_task(_wrap(iid, aid, p)))
            _sync_write()
            await asyncio.sleep(random.uniform(*STAGGER))
            continue
        release.clear()
        try:
            # Короткий timeout — быстрее реагируем на stop_flag
            await asyncio.wait_for(release.wait(), timeout=3)
        except asyncio.TimeoutError:
            pass

    await asyncio.gather(*tasks_list, return_exceptions=True)
    stopped = _likes_stop_requested(task_id)
    stats["status"] = "stopped" if stopped else "done"
    stats["finished_at"] = datetime.utcnow().isoformat() + "Z"
    _likes_status_write(task_id, stats)
    logger.info(f"[leomatch/likes] task #{task_id} {stats['status'].upper()}: {stats['accounts_done']}/{len(rows)} accs, likes={stats['total_likes']}, matches={stats['total_matches']}, fail={stats['fail']}")

    # Сводка в task.error для истории
    summary = (f"[likes/{stats['status']}] processed={stats['accounts_done']}/{len(rows)}, "
               f"total_likes={stats['total_likes']}, matches={stats['total_matches']}, "
               f"failed={stats['fail']}")
    if stats["errs"]:
        summary += "\n  errs (top 10): " + " | ".join(stats["errs"][:10])
    try:
        async with Session() as db:
            await db.execute(sa_update(LeomatchOnboardingTask)
                             .where(LeomatchOnboardingTask.id == task_id)
                             .values(error=summary[:2000]))
            await db.commit()
    except Exception:
        pass

    # Очищаем stop-flag
    try:
        from utils.redis_pool import get_redis
        get_redis().delete(LIKES_STOP_KEY.format(task_id=task_id))
    except Exception:
        pass

    return {"status": stats["status"], "task_id": task_id, **stats}


async def _likes_one(item_id: int, acc_id: int, likes_count: int, stop_check=None):
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload
    from utils.db_pool import async_session as Session
    from models.account import TelegramAccount
    from models.proxy import Proxy
    from utils.telegram import make_telethon_client
    from utils.account_lock import acquire_account_lock, release_account_lock
    from utils.leomatch_bot import send_likes

    async with Session() as db:
        acc = (await db.execute(
            select(TelegramAccount).options(joinedload(TelegramAccount.api_app))
            .where(TelegramAccount.id == acc_id)
        )).scalar_one_or_none()
        if not acc:
            return {"sent": 0, "matches": 0, "error": "acc not found"}
        proxy = (await db.execute(select(Proxy).where(Proxy.id == acc.proxy_id))).scalar_one_or_none() if acc.proxy_id else None

    if not acquire_account_lock(acc.id, ttl=600):
        return {"sent": 0, "matches": 0, "error": "account locked"}

    client = None
    try:
        client = make_telethon_client(acc, proxy_row=proxy)
        if client is None:
            return {"sent": 0, "matches": 0, "error": "no client"}
        await client.connect()
        if not await client.is_user_authorized():
            return {"sent": 0, "matches": 0, "error": "not authorized"}
        # Force-stop check ДО начала — если юзер нажал Stop
        # ещё до того как этот акк дошёл до подключения
        if stop_check and stop_check():
            return {"sent": 0, "matches": 0, "error": "stopped_by_user"}
        return await send_likes(client, likes_count, stop_check=stop_check)
    except Exception as e:
        return {"sent": 0, "matches": 0, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    finally:
        try:
            if client is not None:
                await client.disconnect()
        except Exception:
            pass
        release_account_lock(acc.id)


# ═══════════════════════════════════════════════════════════
# PHOTO REPLACE — прошёл по done items batch'а, у каждого спросил
# @leomatchbot ("Твоя анкета отключена из-за неподходящего фото")
# и если да — заменил фото на случайную вариацию из variations/.
# ═══════════════════════════════════════════════════════════

@celery_app.task(
    name="tasks.leomatch_onboarding.execute_photo_replace",
    time_limit=3600,
    soft_time_limit=3300,
)
def execute_photo_replace(task_id: int):
    return asyncio.run(_execute_photo_replace(task_id))


async def _execute_photo_replace(task_id: int):
    """Идёт по done items, для каждого: /start к боту, если анкета disabled
    → взять случайную вариацию нового фото → заменить через flow бота.
    Consecutive: 2 parallel + 3-6с stagger + proxy-affinity."""
    from pathlib import Path
    from sqlalchemy import select
    from utils.db_pool import async_session as Session
    from models.leomatch_onboarding import LeomatchOnboardingTask, LeomatchOnboardingItem
    from models.account import TelegramAccount

    MAX_PARALLEL = 2   # консервативно — идёт после уже отработавшего онбординга
    STAGGER = (3, 6)

    async with Session() as db:
        rows = (await db.execute(
            select(LeomatchOnboardingItem.id, LeomatchOnboardingItem.account_id,
                   TelegramAccount.proxy_id, LeomatchOnboardingItem.profile)
            .join(TelegramAccount, TelegramAccount.id == LeomatchOnboardingItem.account_id)
            .where(
                LeomatchOnboardingItem.task_id == task_id,
                LeomatchOnboardingItem.status == "done",
            )
            .order_by(LeomatchOnboardingItem.id.asc())
        )).all()

    if not rows:
        return {"status": "no_done_items", "task_id": task_id}

    # Собираем pool вариаций (uploads/leomatch/user_X/variations/*.jpg)
    task_uploads = None
    for r in rows:
        for p in (r[3].get("photos") or []):
            task_uploads = Path(p).parent
            break
        if task_uploads:
            break
    variations_dir = (task_uploads / "variations") if task_uploads else None
    all_variations = list(variations_dir.glob("*.jpg")) if variations_dir and variations_dir.exists() else []
    logger.info(
        f"[leomatch/replace] task #{task_id}: {len(rows)} done items, "
        f"variations pool={len(all_variations)}, concurrency={MAX_PARALLEL}"
    )
    if not all_variations:
        return {"status": "no_variations_pool", "task_id": task_id,
                "hint": f"Ожидается папка {variations_dir}"}

    remaining = [(r[0], r[1], r[2]) for r in rows]
    busy = set()
    in_flight = 0
    release = asyncio.Event()
    tasks_list = []
    stats = {"disabled": 0, "replaced": 0, "ok_already": 0, "fail": 0, "errs": []}

    def _pick():
        for i, (iid, aid, pid) in enumerate(remaining):
            if pid is None or pid not in busy:
                return i, pid
        return None

    async def _wrap(iid, aid, pid):
        nonlocal in_flight
        try:
            r = await _replace_one(iid, aid, all_variations)
            if r["state"] == "replaced":
                stats["disabled"] += 1
                stats["replaced"] += 1
            elif r["state"] == "already_ok":
                stats["ok_already"] += 1
            else:
                stats["fail"] += 1
                stats["errs"].append(f"acc#{aid}: {r.get('error','')[:80]}")
            logger.info(f"[leomatch/replace] acc#{aid}: {r['state']} {r.get('error','')[:80]}")
        finally:
            if pid is not None:
                busy.discard(pid)
            in_flight -= 1
            release.set()

    while remaining or in_flight > 0:
        picked = None
        if in_flight < MAX_PARALLEL:
            picked = _pick()
        if picked is not None:
            idx, pid = picked
            iid, aid, p = remaining.pop(idx)
            if p is not None:
                busy.add(p)
            in_flight += 1
            tasks_list.append(asyncio.create_task(_wrap(iid, aid, p)))
            await asyncio.sleep(random.uniform(*STAGGER))
            continue
        release.clear()
        try:
            await asyncio.wait_for(release.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass

    await asyncio.gather(*tasks_list, return_exceptions=True)
    logger.info(f"[leomatch/replace] task #{task_id} DONE: {stats}")
    return {"status": "done", "task_id": task_id, **stats}


async def _replace_one(item_id: int, acc_id: int, variations_pool: list):
    """Для одного акка: проверить disabled → если да, взять случайную вариацию → replace."""
    import random as _r
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload
    from utils.db_pool import async_session as Session
    from models.account import TelegramAccount
    from models.proxy import Proxy
    from utils.telegram import make_telethon_client
    from utils.account_lock import acquire_account_lock, release_account_lock
    from utils.leomatch_bot import replace_disabled_photo, BOT_USERNAME

    async with Session() as db:
        acc = (await db.execute(
            select(TelegramAccount).options(joinedload(TelegramAccount.api_app))
            .where(TelegramAccount.id == acc_id)
        )).scalar_one_or_none()
        if not acc:
            return {"state": "fail", "error": "acc not found"}
        proxy = (await db.execute(select(Proxy).where(Proxy.id == acc.proxy_id))).scalar_one_or_none() if acc.proxy_id else None

    if not acquire_account_lock(acc.id, ttl=300):
        return {"state": "fail", "error": "account locked"}

    client = None
    try:
        client = make_telethon_client(acc, proxy_row=proxy)
        if client is None:
            return {"state": "fail", "error": "no client"}
        await client.connect()
        if not await client.is_user_authorized():
            return {"state": "fail", "error": "not authorized"}

        bot = await client.get_entity(BOT_USERNAME)
        msgs = await client.get_messages(bot, limit=1)
        last_txt = (msgs[0].message or "").lower() if msgs else ""
        DISABLED = ("анкета была отключена", "анкета отключена", "неподходящего фото", "чужие фото")
        if not any(m in last_txt for m in DISABLED):
            return {"state": "already_ok", "error": ""}

        photo_path = str(_r.choice(variations_pool))
        result = await replace_disabled_photo(client, photo_path)
        if result["success"]:
            return {"state": "replaced", "error": ""}
        return {"state": "fail", "error": f"step={result['step']}: {result['error']}"}
    except Exception as e:
        return {"state": "fail", "error": f"{type(e).__name__}: {str(e)[:200]}"}
    finally:
        try:
            if client is not None:
                await client.disconnect()
        except Exception:
            pass
        release_account_lock(acc.id)


# ═══════════════════════════════════════════════════════════
# PRIVACY FIX — прошёл по всем done items batch'а, у каждого
# делает SetPrivacyRequest(Forwards → AllowAll). Использует
# ТОТ ЖЕ greedy scheduler — не более 4 параллельно, никогда
# 2 акка с одним прокси одновременно.
# ═══════════════════════════════════════════════════════════

@celery_app.task(
    name="tasks.leomatch_onboarding.execute_privacy_fix",
    time_limit=1800,
    soft_time_limit=1700,
)
def execute_privacy_fix(task_id: int):
    return asyncio.run(_execute_privacy_fix(task_id))


async def _execute_privacy_fix(task_id: int):
    from sqlalchemy import select
    from utils.db_pool import async_session as Session
    from models.leomatch_onboarding import LeomatchOnboardingTask, LeomatchOnboardingItem
    from models.account import TelegramAccount

    async with Session() as db:
        task = (await db.execute(select(LeomatchOnboardingTask).where(LeomatchOnboardingTask.id == task_id))).scalar_one_or_none()
        if not task:
            return {"status": "task_not_found"}

        rows = (await db.execute(
            select(LeomatchOnboardingItem.id, LeomatchOnboardingItem.account_id, TelegramAccount.proxy_id)
            .join(TelegramAccount, TelegramAccount.id == LeomatchOnboardingItem.account_id)
            .where(
                LeomatchOnboardingItem.task_id == task_id,
                LeomatchOnboardingItem.status == "done",   # только успешно зарегистрированные
            )
            .order_by(LeomatchOnboardingItem.id.asc())
        )).all()

    if not rows:
        return {"status": "no_done_items", "task_id": task_id}

    logger.info(
        f"[leomatch/privacy] task #{task_id}: {len(rows)} done items, "
        f"unique proxies={len(set(r[2] for r in rows if r[2]))}, "
        f"concurrency={MAX_CONCURRENT}"
    )

    remaining: list[tuple[int, int, int | None]] = list(rows)
    busy_proxies: set[int] = set()
    in_flight = 0
    release_event = asyncio.Event()
    running_tasks: list[asyncio.Task] = []
    stats = {"ok": 0, "fail": 0, "errs": []}

    def _pick_next():
        for i, (iid, acc_id, pid) in enumerate(remaining):
            if pid is None or pid not in busy_proxies:
                return i, pid
        return None

    async def _wrap(item_id: int, acc_id: int, proxy_id: int | None):
        nonlocal in_flight
        try:
            r = await _fix_privacy_item(item_id, acc_id)
            if r["success"]:
                stats["ok"] += 1
            else:
                stats["fail"] += 1
                stats["errs"].append(f"acc#{acc_id}: {r['error'][:80]}")
        finally:
            if proxy_id is not None:
                busy_proxies.discard(proxy_id)
            in_flight -= 1
            release_event.set()

    while remaining or in_flight > 0:
        picked = None
        if in_flight < MAX_CONCURRENT:
            picked = _pick_next()
        if picked is not None:
            idx, proxy_id = picked
            item_id, acc_id, pid = remaining.pop(idx)
            if pid is not None:
                busy_proxies.add(pid)
            in_flight += 1
            running_tasks.append(asyncio.create_task(_wrap(item_id, acc_id, pid)))
            logger.info(f"[leomatch/privacy] fix item#{item_id} acc#{acc_id} proxy#{pid} (in_flight={in_flight}, remaining={len(remaining)})")
            await asyncio.sleep(random.uniform(1, 3))
            continue
        release_event.clear()
        try:
            await asyncio.wait_for(release_event.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass

    await asyncio.gather(*running_tasks, return_exceptions=True)
    logger.info(f"[leomatch/privacy] task #{task_id} done: ok={stats['ok']} fail={stats['fail']}")
    return {"status": "done", "task_id": task_id, "ok": stats["ok"], "fail": stats["fail"], "errs": stats["errs"][:20]}


async def _fix_privacy_item(item_id: int, acc_id: int) -> dict:
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload
    from utils.db_pool import async_session as Session
    from models.account import TelegramAccount
    from models.proxy import Proxy
    from utils.telegram import make_telethon_client
    from utils.account_lock import acquire_account_lock, release_account_lock
    from utils.leomatch_bot import fix_privacy_for_leomatch

    async with Session() as db:
        acc = (await db.execute(
            select(TelegramAccount)
            .options(joinedload(TelegramAccount.api_app))
            .where(TelegramAccount.id == acc_id)
        )).scalar_one_or_none()
        if not acc:
            return {"success": False, "error": "account not found"}
        proxy = (await db.execute(select(Proxy).where(Proxy.id == acc.proxy_id))).scalar_one_or_none() if acc.proxy_id else None

        if not acquire_account_lock(acc.id, ttl=120):
            return {"success": False, "error": "account locked"}
        client = None
        try:
            client = make_telethon_client(acc, proxy_row=proxy)
            if client is None:
                return {"success": False, "error": "make_telethon_client returned None"}
            await client.connect()
            if not await client.is_user_authorized():
                return {"success": False, "error": "not authorized"}
            result = await fix_privacy_for_leomatch(client)
            logger.info(f"[leomatch/privacy] acc#{acc_id}: {'OK' if result['success'] else result['error'][:100]}")
            return result
        except Exception as e:
            return {"success": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
        finally:
            try:
                if client is not None:
                    await client.disconnect()
            except Exception:
                pass
            release_account_lock(acc.id)


async def _mark_finished(task_id: int):
    from sqlalchemy import select
    from utils.db_pool import async_session as Session
    from models.leomatch_onboarding import LeomatchOnboardingTask, LeomatchOnboardingItem
    async with Session() as db:
        t = (await db.execute(select(LeomatchOnboardingTask).where(LeomatchOnboardingTask.id == task_id))).scalar_one_or_none()
        if not t:
            return  # task удалён — нечего финализировать
        items = (await db.execute(select(LeomatchOnboardingItem).where(LeomatchOnboardingItem.task_id == task_id))).scalars().all()
        t.done_items = sum(1 for i in items if i.status == "done")
        t.failed_items = sum(1 for i in items if i.status == "error")
        t.total_items = len(items)
        t.status = "done"
        t.finished_at = datetime.utcnow()
        await db.commit()


def _build_profile_from_json(p: dict):
    """dict → OnboardProfile."""
    from utils.leomatch_bot import OnboardProfile
    return OnboardProfile(
        language=p.get("language") or "🇺🇦 Українська",
        name=p.get("name") or "",
        city=p.get("city") or "Київ",
        description=p.get("description") or "",
        age=int(p.get("age") or 25),
        own_gender=p.get("own_gender") or "auto",
        seek=p.get("seek") or "male",
        photo_paths=list(p.get("photos") or []),
    )
