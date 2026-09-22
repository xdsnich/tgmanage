"""
GramGPT — tasks/plan_executor.py
Выполняет планы кампаний — параллельно, по одной сессии на задачу.

Архитектура:
  dispatch_plans (<1с) → execute_plan_session(plan_id) × N параллельно

Все импорты моделей — внутри функций (lazy import).

ЗАЩИТА ОТ ОСИРОТЕВШИХ ПЛАНОВ:
- dispatch_plans делает JOIN с WarmupTask/Campaign, фильтруя мёртвые
- execute_plan_session перед выполнением проверяет что задача жива и running
- Если задача удалена/остановлена — план помечается как orphan и не выполняется
"""

import asyncio
import sys
import os
import random
import logging
from datetime import datetime, timedelta

from celery_app import celery_app

logger = logging.getLogger(__name__)
API_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── Защита от повторной отправки одной и той же сессии ──────────────
# Диспетчер работает раз в минуту, а сессия идёт 5-20 минут. Пока она идёт,
# указатель executed_idx стоит на месте, время старта уже наступило — и
# диспетчер отправляет ту же сессию снова. Исполнитель же берёт сессию ПО
# УКАЗАТЕЛЮ в момент запуска: первая заканчивается, указатель сдвигается, и
# дубль хватает УЖЕ СЛЕДУЮЩУЮ сессию и выполняет её немедленно.
#
# Замер 28.08: один план запускался по четыре раза за три минуты, и весь
# день расписания сгорал за считанные минуты. У acc#64 сессия, назначенная
# на 03:40, отработала в 00:37.
#
# Метка ставится на пару (план, номер сессии) атомарно через SET NX.
# Живёт INFLIGHT_TTL — этого хватает самой длинной сессии; если исполнитель
# упадёт, метка истечёт сама и сессия будет повторена.
INFLIGHT_TTL = int(os.getenv("PLAN_INFLIGHT_TTL", "1500"))   # 25 минут


def _inflight_key(plan_id: int, idx: int) -> str:
    return f"gramgpt:plan_inflight:{plan_id}:{idx}"


def _try_claim_session(plan_id: int, idx: int) -> bool:
    """True — эту сессию ещё никто не забирал. Fail-open при сбое Redis."""
    try:
        from utils.redis_pool import get_redis
        return bool(get_redis().set(_inflight_key(plan_id, idx), "1",
                                    nx=True, ex=INFLIGHT_TTL))
    except Exception:
        return True


def _release_session(plan_id: int, idx: int):
    """Снять метку сразу по окончании, чтобы следующая сессия не ждала TTL."""
    try:
        from utils.redis_pool import get_redis
        get_redis().delete(_inflight_key(plan_id, idx))
    except Exception:
        pass


async def _safe_log(db, exc=None, **kwargs):
    """Логирует в warmup_logs без краша. Если ошибка — пропускает.

    `exc` — исходное исключение, если ошибка пришла от Telegram. По нему
    (а не по тексту!) проставляется error_origin:
      'telegram' — так сказал Telegram
      'internal' — так решили мы (llm_empty, no_posts и прочие skip-коды)

    Разделение введено 2026-08-22: без него любой счётчик здоровья
    аккаунта наказывал бы его за наши собственные поломки.
    """
    # Опт-аут для мест, где record_error вызывается явно сразу после
    # (smart_comment) — иначе одна ошибка засчиталась бы дважды.
    _skip_health = kwargs.pop('_skip_health', False)
    try:
        from models.warmup_log import WarmupLog
        if 'source' not in kwargs:
            kwargs['source'] = 'warmup'
        if kwargs.get('error') and 'error_origin' not in kwargs:
            try:
                from utils.error_origin import classify_origin
                kwargs['error_origin'] = classify_origin(kwargs.get('error') or "", exc)
            except Exception:
                pass
        db.add(WarmupLog(**kwargs))
        await db.flush()

        # ── Здоровье аккаунта — ЦЕНТРАЛЬНО (2026-08-25) ────────────
        # Инцидент кампании #139: acc#51 получил от Telegram FLOOD_WAIT на
        # 62278с (17ч) на действии join_channel — и через 36 секунд полез
        # снова, потому что record_error звался ровно в двух местах:
        # в ветке smart_comment и в общем обработчике сессии. Флуд на
        # join_channel / join_discussion_of ловился локальным except,
        # писался в лог и на этом всё заканчивалось: отдых не выставлялся,
        # аккаунт продолжал работать и углублял наказание.
        # Теперь любая залогированная ошибка кормит счётчик здоровья.
        # Наши внутренние причины сюда не доходят: record_error действует
        # только на severity='account'.
        if not _skip_health and kwargs.get('account_id') and kwargs.get('error'):
            try:
                from utils.error_origin import classify_severity
                _sv = classify_severity(kwargs.get('error') or "", exc)
                # 'channel' тоже передаём: record_error копит персональные
                # отказы по РАЗНЫМ каналам и по ним обвиняет аккаунт.
                if _sv in ("account", "channel"):
                    from utils.account_health import record_error
                    record_error(kwargs['account_id'], kwargs.get('error') or "", exc,
                                 channel=kwargs.get('channel'))
            except Exception:
                pass
    except Exception:
        try:
            await db.rollback()
        except:
            pass



async def _pick_replacement_channel(db, account_id: int, campaign_id: int,
                                    blocked_now: str, redis_cli) -> str | None:
    """Найти замену каналу, куда этому аккаунту писать нельзя.

    Раньше такой слот просто сгорал: план назначил канал, кэш сказал «сюда
    нельзя», и коммент не писался вовсе. Замер 26.08: у 69 аккаунтов из 87
    есть заблокированные каналы, у худшего закрыто 7 из 19 — то есть треть
    его слотов пропадала.

    Дефицита при этом нет: пригодных каналов 7003, в кампании занято 1714,
    свободных больше пяти тысяч. Логичнее взять другой канал, чем терять
    попытку.

    Берём из УЖЕ НАЗНАЧЕННЫХ аккаунту — там он подписан, вступать заново не
    нужно. Исключаем заблокированные и те, где он сегодня уже писал.
    """
    from sqlalchemy import text as _t
    try:
        rows = (await db.execute(_t("""
            SELECT a.channel_username
            FROM campaign_channel_assignments a
            JOIN channel_capabilities cc ON cc.channel_username = a.channel_username
            WHERE a.campaign_id = :cid AND a.account_id = :aid
              AND cc.commentable = true
              AND a.channel_username <> :bad
              AND NOT EXISTS (
                  SELECT 1 FROM comment_logs cl
                  WHERE cl.account_id = :aid
                    AND cl.channel_username = a.channel_username
                    AND cl.created_at > NOW() - INTERVAL '20 hours')
            ORDER BY random() LIMIT 12
        """), {"cid": campaign_id, "aid": account_id, "bad": blocked_now})).all()
    except Exception:
        return None
    for (uname,) in rows:
        try:
            if redis_cli.get(f"gramgpt:ch_forbidden:{account_id}:{uname}"):
                continue
            if redis_cli.get(f"gramgpt:disc_invite_pending:{account_id}:{uname}"):
                continue
        except Exception:
            pass
        return uname
    return None


def _val(x):
    return x.value if hasattr(x, 'value') else x


# ── Anti-ban helpers (2026-07-27) ────────────────────────────
# Инцидент: acc#185 @reddragon1717 заморожен TG после подряд-резолвов
# несуществующих юзернеймов (@cloudeluxe, @FredMinerr, @vyborg_oskolki).
# После freeze worker продолжал долбить → ещё хуже. Три меры:
# P1: ловим "not available for frozen accounts" в любой ошибке → freeze + stop
# P2: при UsernameNotOccupied/Invalid — сразу помечаем канал is_active=False
# P3: 3 подряд fails join → break сессии + пауза 30 мин (Redis TTL)

_TG_FROZEN_MARKERS = ("not available for frozen accounts", "FROZEN_METHOD_INVALID")
_JOIN_FAIL_PAUSE_KEY = "gramgpt:join_paused:{acc_id}"
_JOIN_FAIL_PAUSE_SEC = 30 * 60


async def _check_tg_freeze(acc, db, error_msg: str, phone: str) -> bool:
    """P1: Telegram сам сказал что акк 'frozen'. Немедленно ставим frozen +
    возвращаем True (caller должен break из session loop)."""
    if not error_msg:
        return False
    if not any(m in error_msg for m in _TG_FROZEN_MARKERS):
        return False
    try:
        from models.account import AccountStatus as _ASt
        acc.status = _ASt.frozen
        try:
            await db.flush()
        except Exception:
            pass
        logger.error(f"[plan][{phone}] TG_FROZEN — акк заморожен телегой, session stop")
        await _safe_log(db, account_id=acc.id, action="error",
                         detail="Telegram сообщил 'frozen accounts' → status=frozen",
                         success=False, error="tg_frozen_account")
    except Exception as _e:
        logger.warning(f"_check_tg_freeze mark err: {_e}")
    return True


async def _blacklist_bad_channel(db, username: str, reason: str):
    """P2: помечаем канал как невалидный. Больше не попадёт ни в один план."""
    from sqlalchemy import update
    clean = (username or "").lstrip("@").strip()
    if not clean:
        return
    try:
        from models.campaign import TargetChannel
        await db.execute(update(TargetChannel).where(
            TargetChannel.username == clean
        ).values(is_active=False))
    except Exception as e:
        logger.warning(f"blacklist TargetChannel @{clean}: {e}")
    try:
        from models.parsed_channel import ParsedChannel
        await db.execute(update(ParsedChannel).where(
            ParsedChannel.username == clean
        ).values(has_comments=False))
    except Exception as e:
        logger.warning(f"blacklist ParsedChannel @{clean}: {e}")
    try:
        from models.campaign_channel_assignment import CampaignChannelAssignment
        await db.execute(update(CampaignChannelAssignment).where(
            CampaignChannelAssignment.channel_username == clean
        ).values(status="failed"))
    except Exception as e:
        logger.warning(f"blacklist CCA @{clean}: {e}")
    try:
        await db.flush()
    except Exception:
        pass
    logger.warning(f"[plan] BLACKLIST @{clean} (reason={reason})")


def _is_join_paused(acc_id: int) -> bool:
    """P3: не пущать в сессию если акк на паузе после 3-х подряд join-fails."""
    try:
        from utils.redis_pool import get_redis
        return bool(get_redis().get(_JOIN_FAIL_PAUSE_KEY.format(acc_id=acc_id)))
    except Exception:
        return False


def _set_join_pause(acc_id: int, phone: str, reason: str):
    try:
        from utils.redis_pool import get_redis
        get_redis().setex(_JOIN_FAIL_PAUSE_KEY.format(acc_id=acc_id), _JOIN_FAIL_PAUSE_SEC, "1")
        logger.error(f"[plan][{phone}] JOIN_PAUSE 30min ({reason})")
    except Exception as e:
        logger.warning(f"_set_join_pause err: {e}")


# ═══════════════════════════════════════════════════════════
# ДИСПЕТЧЕР: находит сессии которые пора выполнить
# ═══════════════════════════════════════════════════════════

async def _dispatch_plans():
    """Лёгкий (<1с): находит campaign_plans с сессиями которые пора выполнить."""
    if API_DIR not in sys.path:
        sys.path.insert(0, API_DIR)

    from sqlalchemy import select, delete as sa_delete, func as sql_func
    from models.campaign_plan import CampaignPlan
    from models.campaign import Campaign, CampaignStatus
    from models.warmup import WarmupTask
    from utils.db_pool import async_session as Session

    dispatched = 0
    skipped = 0
    cleaned = 0

    try:
        async with Session() as db:
            now = datetime.utcnow()
            local_now = now + timedelta(hours=3)  # UTC+3 Lviv
            today = local_now.date()
            current_hour = local_now.hour
            current_minute = local_now.minute

            # ═══ САНИТАРИЯ: удалить осиротевшие планы ═══
            # Планы без живой кампании
            orphan_camp = await db.execute(
                sa_delete(CampaignPlan).where(
                    CampaignPlan.campaign_id != None,
                    CampaignPlan.campaign_id.notin_(select(Campaign.id)),
                )
            )
            if orphan_camp.rowcount:
                cleaned += orphan_camp.rowcount
                logger.warning(f"[plan_dispatch] Удалено осиротевших campaign-планов: {orphan_camp.rowcount}")

            # Планы без живой warmup_task
            orphan_wt = await db.execute(
                sa_delete(CampaignPlan).where(
                    CampaignPlan.warmup_task_id != None,
                    CampaignPlan.warmup_task_id.notin_(select(WarmupTask.id)),
                )
            )
            if orphan_wt.rowcount:
                cleaned += orphan_wt.rowcount
                logger.warning(f"[plan_dispatch] Удалено осиротевших warmup-планов: {orphan_wt.rowcount}")

            if cleaned > 0:
                await db.commit()

            # ═══ SANITIZE user_slots ═══
            # Если счётчик user_slots:N > 0, но НИ ОДНОГО account_lock нет —
            # значит слот «залип» (процесс упал без release). Обнуляем.
            # Особенно важно потому что acquire постоянно сбрасывал TTL
            # (баг 2026-07-13 в utils/user_lock.py fixed inline).
            try:
                from utils.redis_pool import get_redis as _get_r_san
                from utils.user_lock import _PREFIX as _USL_PREFIX, reset_user_slots
                _r_san = _get_r_san()
                _slot_keys = list(_r_san.scan_iter(match=f"{_USL_PREFIX}*"))
                if _slot_keys:
                    _lock_keys = list(_r_san.scan_iter(match="gramgpt:account_lock:*"))
                    _n_locks = len(_lock_keys)
                    for _k in _slot_keys:
                        _kd = _k.decode() if isinstance(_k, bytes) else _k
                        _uid = int(_kd.rsplit(":", 1)[-1])
                        _val_raw = _r_san.get(_k)
                        _val = int(_val_raw or 0)
                        # Если СЛОТОВ больше чем ВСЕХ активных account_locks
                        # (у всех user'ов) — значит точно утечка. Обнуляем.
                        if _val > 0 and _val > _n_locks:
                            logger.warning(
                                f"[plan_dispatch] user_slots:{_uid}={_val} но всего account_locks={_n_locks} → reset"
                            )
                            reset_user_slots(_uid)
            except Exception as _e_san:
                logger.warning(f"[plan_dispatch] user_slots sanitizer err: {_e_san}")

            # ═══ AUTO-START: scheduled кампании после прогрева ═══
            # Условие: либо все WarmupTask батча в finished, либо scheduled_start_at прошло.
            # Реально стартуем — переводим в active, ставим started_at, генерим план на сегодня.
            scheduled_camps = (await db.execute(
                select(Campaign).where(Campaign.status == CampaignStatus.scheduled)
            )).scalars().all()

            for sc in scheduled_camps:
                should_start = False
                reason = ""

                # 1. Все WarmupTask батча закончились?
                if sc.warmup_batch_id:
                    wt_stats = await db.execute(
                        select(
                            WarmupTask.status,
                            sql_func.count(WarmupTask.id).label("cnt"),
                        )
                        .where(WarmupTask.batch_id == sc.warmup_batch_id)
                        .group_by(WarmupTask.status)
                    )
                    rows = wt_stats.all()
                    total = sum(r.cnt for r in rows)
                    finished = sum(r.cnt for r in rows if r.status == "finished")
                    if total > 0 and finished == total:
                        should_start = True
                        reason = f"все {total} прогревов batch={sc.warmup_batch_id} завершены"

                # 2. Fallback по времени
                if not should_start and sc.scheduled_start_at:
                    if sc.scheduled_start_at <= now:
                        should_start = True
                        reason = f"scheduled_start_at={sc.scheduled_start_at.isoformat()} наступило"

                if not should_start:
                    continue

                # Полный запуск: тот же путь что и ручной POST /campaigns/{id}/start.
                # Генерирует CampaignChannelAssignment + CampaignPlan на все дни,
                # ставит status=active + started_at. На следующей итерации
                # dispatch_plans (через 60с) кампания уже подхватит свои планы.
                try:
                    from routers.commenting import _do_start_campaign
                    res = await _do_start_campaign(db, sc)
                    logger.info(f"[plan_dispatch] AUTO-START кампании #{sc.id} '{sc.name}': {reason} → {res.get('message','OK')}")
                except Exception as e:
                    # Если генерация плана упала (нет каналов, нет акков, и т.п.) —
                    # оставляем scheduled, попробуем на следующей итерации.
                    logger.error(f"[plan_dispatch] AUTO-START #{sc.id} '{sc.name}' FAILED: {e}")
                    continue

            # ═══ COMMENTING планы: только для active кампаний ═══
            comm_result = await db.execute(
                select(CampaignPlan)
                .join(Campaign, Campaign.id == CampaignPlan.campaign_id)
                .where(
                    CampaignPlan.plan_date == today,
                    CampaignPlan.status == "active",
                    Campaign.status == CampaignStatus.active,
                )
            )
            plans = list(comm_result.scalars().all())

            # ═══ WARMUP планы: только для running задач ═══
            # Прогрев исполняется ЕДИНЫМ движком — plan_executor (как комментинг).
            # Родной warmup_v2-движок отключён (см. celery_app beat). Так план
            # ВСЕГДА совпадает с выполнением — нет двух источников правды.
            warmup_result = await db.execute(
                select(CampaignPlan)
                .join(WarmupTask, WarmupTask.id == CampaignPlan.warmup_task_id)
                .where(
                    CampaignPlan.plan_date == today,
                    CampaignPlan.status == "active",
                    CampaignPlan.campaign_id == None,
                    CampaignPlan.warmup_task_id != None,
                    WarmupTask.status == "running",
                )
            )
            plans.extend(list(warmup_result.scalars().all()))

            # ═══ ФИЛЬТРАЦИЯ: собираем только готовые ═══
            # Раньше делали send_task прямо в цикле, и при batch'е 100+
            # planов это превращалось в thundering herd (см. инцидент
            # 2026-06-12: 166 Telethon-сессий за 2 сек, 5-6 акков на один
            # IP синхронно → Telegram mass revoke).
            # Теперь: сначала собираем все ready planы, потом размазываем
            # старты по времени через countdown с jitter'ом.
            ready_plans = []
            for plan in plans:
                sessions = plan.plan.get("sessions", [])
                if plan.executed_idx >= len(sessions):
                    plan.status = "done"
                    skipped += 1
                    continue

                next_session = sessions[plan.executed_idx]

                if next_session.get("skipped"):
                    plan.executed_idx += 1
                    logger.info(f"[plan] Пропуск сессии #{plan.executed_idx} (акк {plan.account_id}): {next_session.get('skip_reason', '?')}")
                    if plan.executed_idx >= len(sessions):
                        plan.status = "done"
                    skipped += 1
                    continue

                sess_hour = next_session.get("connect_at_hour", 0)
                sess_min = next_session.get("connect_at_minute", 0)

                if current_hour < sess_hour:
                    skipped += 1
                    continue
                if current_hour == sess_hour and current_minute < sess_min:
                    skipped += 1
                    continue

                # Не слишком ли поздно? Ждём сессию до 4 часов (240 мин) —
                # раньше было 120 мин, но при 5+ акках на IP и cooldown 10-15 мин
                # сессии часто откладывались > 2ч из-за ip_cooldown циклов
                # и просто выкидывались с потерей smart_comment (инцидент 2026-07-14:
                # 126 smart_comment потеряно за день из 285 запланированных).
                sess_total_min = sess_hour * 60 + sess_min
                curr_total_min = current_hour * 60 + current_minute
                if curr_total_min - sess_total_min > 240:
                    plan.executed_idx += 1
                    logger.info(f"[plan] Пропуск просроченной сессии >4ч (акк {plan.account_id})")
                    if plan.executed_idx >= len(sessions):
                        plan.status = "done"
                    skipped += 1
                    continue

                ready_plans.append(plan)

            # ═══ STAGGER ═══
            # Размазываем старты в окне countdown_window_sec, чтобы НИКОГДА
            # не было burst'а. Размер окна растёт с размером батча, но
            # ограничен сверху (DISPATCH_STAGGER_MAX_SEC) — иначе очень
            # долгий tail.
            # Формула: window = max(MIN, min(MAX, N * PER_PLAN_SEC))
            # Дефолты подобраны так, чтобы при IP_COOLDOWN_SEC=900 (15 мин)
            # и ~34 прокси на одном IP реально стартовало ≤ 1 акка
            # за окно cooldown'а: 200 planов × 30с = 100 мин окна,
            # делим на 34 прокси = ~3 минуты между стартами на один IP.
            import random as _rand
            _rand.shuffle(ready_plans)
            stagger_min  = int(os.getenv("DISPATCH_STAGGER_MIN_SEC", "30"))
            stagger_max  = int(os.getenv("DISPATCH_STAGGER_MAX_SEC", "3600"))
            per_plan_sec = float(os.getenv("DISPATCH_STAGGER_PER_PLAN_SEC", "30"))
            window_sec = max(stagger_min, min(stagger_max, int(len(ready_plans) * per_plan_sec)))

            # ── 2026-08-22: планы с расписанием по IP не размазываем ──
            # Большой stagger существовал потому, что расписания не было —
            # он гасил burst вслепую, случайным сдвигом до часа. Но для
            # плана, чьи времена согласованы ip_scheduler'ом с соседями по
            # тому же IP, этот сдвиг РАЗРУШАЕТ гарантию зазора: два
            # аккаунта, разведённые на 25 минут, могут стартовать
            # одновременно. Таким планам даём только микро-джиттер.
            _scheduled = [p for p in ready_plans if (p.plan or {}).get("ip_scheduled")]
            _legacy    = [p for p in ready_plans if not (p.plan or {}).get("ip_scheduled")]
            _legacy_window = max(stagger_min, min(stagger_max, int(len(_legacy) * per_plan_sec)))
            micro_jitter = int(os.getenv("DISPATCH_SCHEDULED_JITTER_SEC", "90"))

            if ready_plans:
                logger.info(
                    f"[plan_dispatch] {len(_scheduled)} планов по IP-расписанию "
                    f"(джиттер ±{micro_jitter}с), {len(_legacy)} старых "
                    f"размазываем по {_legacy_window}с"
                )

            for plan in _scheduled:
                if not _try_claim_session(plan.id, plan.executed_idx):
                    skipped += 1
                    continue
                countdown = _rand.uniform(0, micro_jitter)
                celery_app.send_task(
                    "tasks.plan_executor.execute_plan_session",
                    args=[plan.id],
                    queue="plans",
                    countdown=countdown,
                )
                dispatched += 1

            for plan in _legacy:
                if not _try_claim_session(plan.id, plan.executed_idx):
                    skipped += 1
                    continue
                # Full jitter: countdown ∈ [0, window_sec]
                countdown = _rand.uniform(0, _legacy_window)
                celery_app.send_task(
                    "tasks.plan_executor.execute_plan_session",
                    args=[plan.id],
                    queue="plans",
                    countdown=countdown,
                )
                dispatched += 1

            # ═══ AUTO-CLOSE кампаний ═══
            from sqlalchemy import func
            active_campaigns = (await db.execute(
                select(Campaign).where(Campaign.status == CampaignStatus.active)
            )).scalars().all()

            logger.info(f"[autoclose] Проверка {len(active_campaigns)} активных кампаний")

            for camp in active_campaigns:
                logger.info(f"[autoclose]   camp {camp.id}: started_at={camp.started_at}, max_hours={camp.max_hours}, comments={camp.comments_sent}/{camp.max_comments}")

                # check_only кампании НИКОГДА не закрываются по времени —
                # user настоял, screening-режим должен работать пока не
                # обработает все каналы (2026-07-14).
                _is_check_only = getattr(camp, "campaign_type", "regular") == "check_only"

                # 1. Время истекло? (только для regular)
                if not _is_check_only and camp.started_at and camp.max_hours and camp.max_hours > 0:
                    started = camp.started_at
                    if hasattr(started, 'tzinfo') and started.tzinfo is not None:
                        started = started.replace(tzinfo=None)
                    now_utc = datetime.utcnow()
                    delta = now_utc - started
                    elapsed_sec = delta.total_seconds()

                    if elapsed_sec < 0:
                        logger.warning(f"[autoclose]   camp {camp.id}: started_at В БУДУЩЕМ! — пропуск")
                        continue

                    elapsed_hours = elapsed_sec / 3600
                    logger.info(f"[autoclose]   camp {camp.id}: прошло {elapsed_hours:.2f}ч / {camp.max_hours}ч")

                    if elapsed_hours >= camp.max_hours:
                        camp.status = CampaignStatus.finished
                        if not getattr(camp, 'finished_at', None):
                            camp.finished_at = datetime.utcnow()
                        logger.info(f"[autoclose] Кампания {camp.id} → finished (время истекло: {elapsed_hours:.1f}ч / {camp.max_hours}ч)")
                        continue

                # 2. Лимит комментов достигнут?
                if camp.max_comments and camp.comments_sent >= camp.max_comments:
                    camp.status = CampaignStatus.finished
                    if not getattr(camp, 'finished_at', None):
                        camp.finished_at = datetime.utcnow()
                    logger.info(f"[autoclose] Кампания {camp.id} → finished (лимит комментов {camp.comments_sent}/{camp.max_comments})")
                    continue

                # 3. Все планы выполнены?
                #
                # Только для кампаний, у которых планы вообще бывают.
                # У realtime-кампании их нет по устройству: она пишет по
                # факту выхода поста, а не по расписанию. Без этой
                # оговорки она закрывалась в первую же минуту после
                # запуска — «планов ноль, значит всё сделано».
                if getattr(camp, "campaign_type", "regular") in ("realtime",
                                                                 "check_only"):
                    continue

                remaining = (await db.execute(
                    select(func.count(CampaignPlan.id)).where(
                        CampaignPlan.campaign_id == camp.id,
                        CampaignPlan.status == "active",
                    )
                )).scalar() or 0

                if remaining == 0:
                    future = (await db.execute(
                        select(func.count(CampaignPlan.id)).where(
                            CampaignPlan.campaign_id == camp.id,
                            CampaignPlan.plan_date > today,
                        )
                    )).scalar() or 0

                    if future == 0:
                        camp.status = CampaignStatus.finished
                        if not getattr(camp, 'finished_at', None):
                            camp.finished_at = datetime.utcnow()
                        logger.info(f"[autoclose] Кампания {camp.id} → finished (все планы выполнены)")

            await db.commit()
            logger.info(f"[plan_dispatch] Отправлено {dispatched}, пропущено {skipped}, очищено {cleaned}")

    except Exception as e:
        logger.error(f"[plan_dispatch] Ошибка: {e}")

    return {"dispatched": dispatched, "skipped": skipped, "cleaned": cleaned}


# ═══════════════════════════════════════════════════════════
# EXECUTOR: выполняет одну сессию одного аккаунта
# ═══════════════════════════════════════════════════════════

RT_YIELD_MAX = int(os.getenv("PLAN_RT_YIELD_MAX", "3"))


async def _realtime_waiting(db, account_id: int, plan_id: int, idx: int) -> bool:
    """Ждёт ли у этого аккаунта realtime-задание, ради которого стоит
    уступить адрес.

    Зачем. Оба движка ходят через один и тот же прокси, а тот после
    подключения остывает десять минут. Плановая сессия и realtime-задание
    равны для замка, но не равны по цене: сессию можно провести и через
    полчаса, ничего не потеряв, а комментарий под свежим постом через
    полчаса не нужен вовсе — в этом и был весь смысл второго движка.
    Поэтому при равных правах уступает плановая.

    Уступаем не бесконечно. Наблюдатель держит по заданию на аккаунт
    постоянно, переписывая его на свежие посты, так что «есть задание»
    верно почти всегда. Без потолка плановые сессии этих аккаунтов не
    состоялись бы уже никогда. RT_YIELD_MAX уступок на сессию — дальше
    идём вперёд: у realtime суточная норма 10 комментов, у плана 5
    сессий, а адрес выдаёт 144 подключения в сутки, места хватает обоим.
    """
    from sqlalchemy import text as sa_text

    row = (await db.execute(sa_text("""
        SELECT 1 FROM comment_queue q
        JOIN campaigns c ON c.id = q.campaign_id
        WHERE q.account_id = :a AND q.status = 'scheduled'
          AND c.campaign_type = 'realtime'
          AND q.scheduled_at <= (NOW() AT TIME ZONE 'utc')
        LIMIT 1"""), {"a": account_id})).first()
    if not row:
        return False

    try:
        from utils.redis_pool import get_redis
        r = get_redis()
        k = "gramgpt:plan_rt_yield:%d:%d" % (plan_id, idx)
        n = r.incr(k)
        r.expire(k, 6 * 3600)
        if n > RT_YIELD_MAX:
            logger.info("[plan] plan #%d: уступал realtime %d раз — иду вперёд",
                        plan_id, n - 1)
            return False
    except Exception:
        return False
    return True


async def _execute_plan_session(plan_id: int):
    """Выполняет одну сессию из плана."""
    if API_DIR not in sys.path:
        sys.path.insert(0, API_DIR)

    from sqlalchemy import select
    from sqlalchemy.orm import joinedload
    from models.campaign_plan import CampaignPlan
    from models.campaign import Campaign, CampaignStatus, CommentLog
    from models.account import TelegramAccount
    from models.proxy import Proxy
    from models.warmup_log import WarmupLog
    from models.warmup import WarmupTask
    from utils.telegram import make_telethon_client
    from utils.account_lock import acquire_account_lock, release_account_lock
    from utils.user_lock import acquire_user_slot, release_user_slot
    from utils.ip_throttle import acquire_ip_lock, get_ip_cooldown_remaining
    from utils.db_pool import async_session as Session
    from services.llm import generate_comment, build_comment_prompt
    from tasks.behavior_engine import assign_style_profile

    async with Session() as db:
        try:
            # ── Загружаем план ────────────────────────────
            plan = (await db.execute(
                select(CampaignPlan).where(CampaignPlan.id == plan_id)
            )).scalar_one_or_none()

            if not plan:
                logger.warning(f"[plan_executor] Plan #{plan_id} не найден — пропуск")
                return {"status": "plan_not_found"}

            if plan.status != "active":
                logger.info(f"[plan_executor] Plan #{plan_id} status={plan.status} — пропуск")
                return {"status": "not_active"}

            # Определяем тип плана: warmup или commenting
            plan_source = 'warmup' if plan.warmup_task_id else 'commenting'
            plan_campaign_id = plan.campaign_id if not plan.warmup_task_id else None
            # Для warmup логи должны иметь task_id (иначе невидимы в UID задачи)
            log_task_id = plan.warmup_task_id if plan.warmup_task_id else None

            # ═══ ПРОВЕРКА ЖИВОСТИ СВЯЗАННОЙ ЗАДАЧИ ═══
            if plan.warmup_task_id:
                wt = (await db.execute(
                    select(WarmupTask).where(WarmupTask.id == plan.warmup_task_id)
                )).scalar_one_or_none()
                if not wt:
                    logger.warning(f"[plan_executor] Plan #{plan_id} — WarmupTask {plan.warmup_task_id} УДАЛЕНА. Удаляем план.")
                    await db.delete(plan)
                    await db.commit()
                    return {"status": "orphan_warmup_task"}
                if wt.status != "running":
                    logger.info(f"[plan_executor] Plan #{plan_id} — WarmupTask {plan.warmup_task_id} status={wt.status}, пропуск")
                    return {"status": "warmup_not_running"}
            elif plan.campaign_id:
                camp = (await db.execute(
                    select(Campaign).where(Campaign.id == plan.campaign_id)
                )).scalar_one_or_none()
                if not camp:
                    logger.warning(f"[plan_executor] Plan #{plan_id} — Campaign {plan.campaign_id} УДАЛЕНА. Удаляем план.")
                    await db.delete(plan)
                    await db.commit()
                    return {"status": "orphan_campaign"}
                if _val(camp.status) != "active":
                    logger.info(f"[plan_executor] Plan #{plan_id} — Campaign {plan.campaign_id} status={_val(camp.status)}, пропуск")
                    return {"status": "campaign_not_active"}

            logger.info(f"[plan_executor] Plan #{plan_id} тип: {plan_source.upper()}")

            sessions = plan.plan.get("sessions", [])
            if plan.executed_idx >= len(sessions):
                plan.status = "done"
                await db.commit()
                return {"status": "done"}

            session = sessions[plan.executed_idx]
            _claim_idx = plan.executed_idx

            # ── Вторая линия обороны: не бежать впереди расписания ──
            # Метка в диспетчере уже не даёт слать дубли, но исполнитель
            # берёт сессию по указателю в момент запуска, а не ту, что ему
            # назначили. Любая задача, дождавшаяся своей очереди позже, чем
            # сдвинулся указатель, взяла бы следующую сессию досрочно.
            # Проверяем время здесь ещё раз: своё — работаем, чужое —
            # выходим, диспетчер вернётся когда наступит.
            _now_loc = datetime.utcnow() + timedelta(hours=3)      # Киев, UTC+3
            _due = session.get("connect_at_hour", 0) * 60 + session.get("connect_at_minute", 0)
            _cur = _now_loc.hour * 60 + _now_loc.minute
            if _cur < _due - 2:            # 2 минуты допуска на дрожание
                logger.info(
                    f"[plan_executor] Plan #{plan_id} сессия #{plan.executed_idx + 1} "
                    f"назначена на {_due // 60:02d}:{_due % 60:02d}, сейчас "
                    f"{_now_loc.hour:02d}:{_now_loc.minute:02d} — рано, откладываю"
                )
                _release_session(plan_id, _claim_idx)
                return {"status": "too_early",
                        "due": f"{_due // 60:02d}:{_due % 60:02d}"}

            if session.get("skipped"):
                plan.executed_idx += 1
                if plan.executed_idx >= len(sessions):
                    plan.status = "done"
                await db.commit()
                return {"status": "skipped"}

            actions = session.get("actions", [])
            if not actions:
                plan.executed_idx += 1
                await db.commit()
                return {"status": "empty_session"}

            # ── P3: пауза после 3-х подряд join-фейлов ─────
            if _is_join_paused(plan.account_id):
                logger.info(f"[plan] acc#{plan.account_id} на join-pause (30мин cooldown) — пропуск")
                return {"status": "join_paused"}

            # ── ОТДЫХ по претензии Telegram (2026-08-22) ───
            # Ставится ТОЛЬКО из utils/account_health.record_error и только
            # когда severity='account' — то есть Telegram отчитал именно
            # аккаунт (PEER_FLOOD, крупный FLOOD_WAIT, USER_RESTRICTED).
            # Наши внутренние причины (llm_empty, no_posts) сюда не доходят.
            from utils.account_health import is_resting, rest_remaining
            _resting, _rest_reason = is_resting(plan.account_id)
            if _resting:
                logger.info(
                    f"[plan] acc#{plan.account_id} отдыхает ещё "
                    f"{rest_remaining(plan.account_id) // 60}мин ({_rest_reason}) — пропуск"
                )
                return {"status": "resting", "reason": _rest_reason}

            # ── Lock аккаунта ─────────────────────────────
            # try_acquire_connection АТОМАРНО занимает суточный слот
            # (см. фикс гонки в utils/connection_limiter). Если дальше
            # сессия не состоится — слот возвращаем через release_connection.
            from utils.connection_limiter import try_acquire_connection, release_connection
            if not try_acquire_connection(plan.account_id):
                return {"status": "daily_limit"}
            _conn_slot_taken = True
            _session_never_started = False

            if not acquire_account_lock(plan.account_id, ttl=1800):
                logger.info(f"[plan] Аккаунт {plan.account_id} занят (lock) — пропуск")
                release_connection(plan.account_id)
                return {"status": "locked"}

            slot_user_id = None  # для release в finally
            try:
                acc = (await db.execute(
                    select(TelegramAccount).options(joinedload(TelegramAccount.api_app))
                    .where(TelegramAccount.id == plan.account_id)
                )).scalar_one_or_none()

                if not acc or acc.status not in ("active", "unknown"):
                    plan.executed_idx += 1
                    await db.commit()
                    release_connection(plan.account_id)
                    return {"status": "inactive"}

                # Per-user concurrency limit — защита от того что один юзер
                # с 500 аккаунтами съест всю мощность воркера
                if not acquire_user_slot(acc.user_id):
                    logger.info(f"[plan] User {acc.user_id}: лимит одновременных сессий, пропуск")
                    release_connection(plan.account_id)
                    return {"status": "user_at_limit"}
                slot_user_id = acc.user_id

                proxy = None
                if acc.proxy_id:
                    proxy = (await db.execute(
                        select(Proxy).where(Proxy.id == acc.proxy_id)
                    )).scalar_one_or_none()

                # ═══ ПРИОРИТЕТ REALTIME ═══
                # Адрес один на оба движка. Уступаем его тому, для кого
                # время критично: комментарий под только что вышедшим
                # постом протухнет за полчаса, плановая сессия — нет.
                # Уступок на сессию не больше RT_YIELD_MAX, иначе планы
                # этих аккаунтов не состоялись бы никогда.
                if await _realtime_waiting(db, plan.account_id,
                                           plan.id, _claim_idx):
                    defer_sec = random.uniform(300, 600)
                    logger.info(
                        f"[plan] plan #{plan.id}: у acc#{plan.account_id} ждёт "
                        f"realtime-задание, уступаю адрес на {defer_sec:.0f}с"
                    )
                    release_user_slot(acc.user_id)
                    slot_user_id = None
                    release_account_lock(plan.account_id)
                    release_connection(plan.account_id)
                    celery_app.send_task(
                        "tasks.plan_executor.execute_plan_session",
                        args=[plan.id], queue="plans", countdown=defer_sec)
                    return {"status": "realtime_priority",
                            "defer_sec": int(defer_sec)}

                # ═══ PER-IP COOLDOWN (anti-burst) ═══
                # Защита от инцидента 2026-06-12: на одном IP не должно
                # быть второй Telegram-сессии ближе чем через 10-15 мин.
                # Если IP занят — НЕ запускаем сессию сейчас, а отправляем
                # план в очередь снова с countdown = оставшийся TTL + jitter.
                # Аккаунт-lock и user-slot отпускаем — пусть другие
                # планы их используют.
                if proxy and not acquire_ip_lock(proxy):
                    remaining = get_ip_cooldown_remaining(proxy)
                    defer_sec = remaining + random.uniform(30, 180)
                    logger.info(
                        f"[plan] IP {proxy.host}:{proxy.port} в cooldown "
                        f"({remaining}с), откладываю plan #{plan.id} на {defer_sec:.0f}с"
                    )
                    release_user_slot(acc.user_id)
                    slot_user_id = None
                    release_account_lock(plan.account_id)
                    # Сессия не состоялась — возвращаем суточный слот,
                    # иначе отложенные из-за IP-cooldown планы сжигали бы
                    # лимит подключений впустую.
                    release_connection(plan.account_id)
                    celery_app.send_task(
                        "tasks.plan_executor.execute_plan_session",
                        args=[plan.id],
                        queue="plans",
                        countdown=defer_sec,
                    )
                    return {"status": "ip_cooldown", "defer_sec": int(defer_sec)}

                # Объявляем client до try, чтобы finally:disconnect()
                # никогда не падал на NameError. До этого момента upstream
                # exception оставлял sqlite-сессию залоченной (WinError 32
                # в logs/api.log 2026-06-12), потому что finally падал и
                # последующий disconnect не выполнялся.
                client = make_telethon_client(acc, proxy)
                if not client:
                    plan.executed_idx += 1
                    await db.commit()
                    release_connection(plan.account_id)
                    return {"status": "no_client"}

                campaign = None
                if plan.campaign_id:
                    campaign = (await db.execute(
                        select(Campaign).where(Campaign.id == plan.campaign_id)
                    )).scalar_one_or_none()

                phone = acc.phone
                done_actions = 0
                sess_num = plan.executed_idx + 1
                total_sess = len(sessions)
                mood = plan.plan.get('mood', '?')

                logger.info(f"[plan][{phone}] ═══ Сессия {sess_num}/{total_sess} (день {plan.day_number}, {mood}) ═══")

                await _safe_log(db, task_id=log_task_id, account_id=acc.id, action="session_start",
                                 detail=f"Старт сессии {sess_num}/{total_sess} (день {plan.day_number}, {mood})",
                                 success=True, source=plan_source, campaign_id=plan_campaign_id)

                try:
                    await client.connect()
                    # Слот подключения уже занят атомарно в try_acquire_connection
                    # выше — повторный increment здесь давал бы двойной счёт.
                    # История подключений в БД
                    from services.connection_logger import log_connection
                    await log_connection(db, plan.account_id, source=plan_source, proxy_id=proxy.id if proxy else None)
                    if not await client.is_user_authorized():
                        plan.executed_idx += 1
                        logger.warning(f"[plan][{phone}] Не авторизован")
                        await _safe_log(db, task_id=log_task_id, account_id=acc.id, action="error",
                                         detail="Не авторизован", success=False,
                                         source=plan_source, campaign_id=plan_campaign_id)
                        await db.commit()
                        return {"status": "not_authorized"}

                    logger.info(f"[plan][{phone}] Подключен, {len(actions)} действий")

                    # P3: session-level счётчик подряд-фейлов на join_*
                    _join_fail_count = 0

                    for action_num, action in enumerate(actions, 1):
                        # ── Telegram отчитал аккаунт прямо в этой сессии? ──
                        # _safe_log кормит счётчик здоровья на КАЖДОЙ ошибке,
                        # и record_error может выставить отдых посреди сессии.
                        # Без этой проверки аккаунт доигрывал оставшиеся
                        # действия уже под флудом: acc#51 словил FLOOD_WAIT
                        # 17ч на join_channel и через 36с полез в следующий
                        # join. Проверка локальная (Redis), в TG не ходит.
                        try:
                            from utils.account_health import is_resting as _is_rest_now
                            _rest_now, _rest_why = _is_rest_now(acc.id)
                            if _rest_now:
                                logger.warning(
                                    f"[plan][{phone}]   сессия прервана на "
                                    f"[{action_num}/{len(actions)}]: {_rest_why}"
                                )
                                break
                        except Exception:
                            pass

                        try:
                            action_type = action.get("type", "")

                            if action_type == "idle":
                                dur = action.get("duration", 30)
                                logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] ⏸ Пауза {dur}с")
                                await asyncio.sleep(dur)
                                continue

                            elif action_type == "read_feed":
                                channel_name = action.get("channel")
                                count = action.get("count", 5)
                                try:
                                    if channel_name:
                                        entity = await client.get_entity(channel_name)
                                    else:
                                        dialogs = await client.get_dialogs(limit=15)
                                        if dialogs:
                                            entity = random.choice(dialogs[:10])
                                        else:
                                            continue

                                    msgs = await client.get_messages(entity, limit=count)
                                    for m in msgs:
                                        await client.send_read_acknowledge(entity, m)
                                        await asyncio.sleep(random.uniform(0.5, 4))

                                    name = channel_name or getattr(entity, 'name', '?') or getattr(entity, 'title', '?')
                                    logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] 📖 Прочитал {len(msgs)} постов в «{name}»")
                                    await _safe_log(db, task_id=log_task_id, account_id=acc.id, action="read_feed",
                                                     detail=f"Прочитал {len(msgs)} в «{name}»",
                                                     channel=str(name), success=True,
                                                     source=plan_source, campaign_id=plan_campaign_id)
                                    done_actions += 1
                                except Exception as e:
                                    logger.warning(f"[plan][{phone}]   [{action_num}/{len(actions)}] 📖 read_feed ошибка: {e}")

                            elif action_type == "view_stories":
                                try:
                                    from telethon.tl.functions.stories import GetAllReadPeerStoriesRequest
                                    await client(GetAllReadPeerStoriesRequest())
                                    count = action.get("count", 3)
                                    logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] 👁 Stories ({count})")
                                    await _safe_log(db, task_id=log_task_id, account_id=acc.id, action="view_stories",
                                                     detail=f"Просмотрел stories ({count})", success=True,
                                                     source=plan_source, campaign_id=plan_campaign_id)
                                    done_actions += 1
                                except Exception:
                                    pass

                            elif action_type == "set_reaction":
                                try:
                                    from telethon.tl.functions.messages import SendReactionRequest
                                    from telethon.tl.types import ReactionEmoji
                                    from telethon.tl.functions.channels import GetFullChannelRequest
                                    from telethon.errors import ReactionInvalidError
                                    channel_name = action.get("channel")

                                    if channel_name:
                                        entity = await client.get_entity(channel_name)
                                    else:
                                        dialogs = await client.get_dialogs(limit=10)
                                        channels = [d for d in dialogs if getattr(d, 'is_channel', False)]
                                        if not channels:
                                            continue
                                        entity = random.choice(channels)

                                    # Разбираем available_reactions канала.
                                    # ChatReactionsSome → только из whitelist
                                    # ChatReactionsAll / None → весь стандартный набор
                                    # ChatReactionsNone → реакции запрещены, скип
                                    allowed_emojis = []
                                    skip = False
                                    try:
                                        full = await client(GetFullChannelRequest(entity))
                                        available = getattr(full.full_chat, 'available_reactions', None)
                                        cls = type(available).__name__ if available is not None else "None"
                                        if cls == "ChatReactionsSome":
                                            for r in getattr(available, 'reactions', []):
                                                if hasattr(r, 'emoticon'):
                                                    allowed_emojis.append(r.emoticon)
                                        elif cls == "ChatReactionsNone":
                                            skip = True  # явно запрещены
                                        else:
                                            # ChatReactionsAll или None (legacy) → стандартные
                                            allowed_emojis = ["👍", "👎", "❤️", "🔥", "🥰", "👏", "😁",
                                                              "🤔", "🤯", "😱", "🤬", "😢", "🎉", "🤩", "🙏", "💯"]
                                    except Exception:
                                        allowed_emojis = ["👍", "❤️", "🔥", "👏"]

                                    if skip or not allowed_emojis:
                                        continue

                                    msgs = await client.get_messages(entity, limit=5)
                                    if not msgs:
                                        continue
                                    target_msg = random.choice(msgs)

                                    # Пробуем до 3 разных эмодзи — если канал отверг один, берём другой
                                    candidates = random.sample(allowed_emojis, min(3, len(allowed_emojis)))
                                    reacted_emoji = None
                                    for emoji in candidates:
                                        try:
                                            await client(SendReactionRequest(
                                                peer=entity, msg_id=target_msg.id,
                                                reaction=[ReactionEmoji(emoticon=emoji)]
                                            ))
                                            reacted_emoji = emoji
                                            break
                                        except ReactionInvalidError:
                                            continue

                                    if reacted_emoji:
                                        name = channel_name or getattr(entity, 'name', '?') or getattr(entity, 'title', '?')
                                        logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] 😍 Реакция {reacted_emoji} в «{name}»")
                                        await _safe_log(db, task_id=log_task_id, account_id=acc.id, action="set_reaction",
                                                         detail=f"Реакция {reacted_emoji} в «{name}»",
                                                         channel=str(name), emoji=reacted_emoji, success=True,
                                                         source=plan_source, campaign_id=plan_campaign_id)
                                        done_actions += 1
                                except Exception as e:
                                    logger.warning(f"[plan][{phone}]   [{action_num}/{len(actions)}] 😍 reaction ошибка: {e}")

                            elif action_type == "view_profile":
                                try:
                                    dialogs = await client.get_dialogs(limit=15)
                                    users = [d for d in dialogs if getattr(d, 'is_user', False)]
                                    if users:
                                        u = random.choice(users[:5])
                                        from telethon.tl.functions.users import GetFullUserRequest
                                        await client(GetFullUserRequest(u.input_entity))
                                        logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] 👤 Профиль «{u.name or '?'}»")
                                        await _safe_log(db, task_id=log_task_id, account_id=acc.id, action="view_profile",
                                                         detail=f"Просмотрел профиль «{u.name or '?'}»", success=True,
                                                         source=plan_source, campaign_id=plan_campaign_id)
                                        done_actions += 1
                                except Exception:
                                    pass

                            elif action_type == "search":
                                try:
                                    from telethon.tl.functions.contacts import SearchRequest
                                    terms = ["crypto", "news", "music", "sport", "tech", "games", "memes", "trade"]
                                    term = random.choice(terms)
                                    await client(SearchRequest(q=term, limit=5))
                                    logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] 🔍 Поиск «{term}»")
                                    await _safe_log(db, task_id=log_task_id, account_id=acc.id, action="search",
                                                     detail=f"Поиск «{term}»", success=True,
                                                     source=plan_source, campaign_id=plan_campaign_id)
                                    done_actions += 1
                                except Exception:
                                    pass

                            elif action_type == "send_saved":
                                try:
                                    text = action.get("text", "📌")
                                    me = await client.get_me()
                                    await client.send_message(me, text)
                                    logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] 💬 Saved: {text}")
                                    await _safe_log(db, task_id=log_task_id, account_id=acc.id, action="send_saved",
                                                     detail=f"Saved: {text}", success=True,
                                                     source=plan_source, campaign_id=plan_campaign_id)
                                    done_actions += 1
                                except Exception:
                                    pass

                            elif action_type == "forward_saved":
                                try:
                                    dialogs = await client.get_dialogs(limit=10)
                                    channels = [d for d in dialogs if getattr(d, 'is_channel', False)]
                                    if channels:
                                        ch = random.choice(channels)
                                        msgs = await client.get_messages(ch, limit=5)
                                        if msgs:
                                            me = await client.get_me()
                                            await client.forward_messages(me, msgs[0])
                                            logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] 💾 Переслал из «{ch.name or '?'}»")
                                            await _safe_log(db, task_id=log_task_id, account_id=acc.id, action="forward_saved",
                                                             detail=f"Переслал из «{ch.name or '?'}»", success=True,
                                                             source=plan_source, campaign_id=plan_campaign_id)
                                            done_actions += 1
                                except Exception:
                                    pass

                            elif action_type == "post_story":
                                # Постит рандомное фото из api/account_media/{account_id}/
                                # в сториз. Premium-only — мягкий скип на не-Premium.
                                # Жёсткий лимит: 1 сториз в сутки (через Redis-счётчик).
                                try:
                                    from utils.story_limit import can_post_story_today, mark_story_posted_today
                                    if not can_post_story_today(acc.id):
                                        logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] 📸 Сториз: уже постили сегодня, скип")
                                        continue

                                    from routers.account_media import list_account_media_paths
                                    paths = list_account_media_paths(acc.id)
                                    if not paths:
                                        logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] 📸 Сториз: папка пустая, скип")
                                        continue
                                    photo_path = random.choice(paths)
                                    try:
                                        from telethon.tl.functions.stories import SendStoryRequest
                                        from telethon.tl.types import InputMediaUploadedPhoto, InputPrivacyValueAllowAll
                                    except ImportError:
                                        logger.warning(f"[plan][{phone}] stories API недоступен в текущей Telethon")
                                        continue
                                    try:
                                        f = await client.upload_file(str(photo_path))
                                        media = InputMediaUploadedPhoto(file=f)
                                        await client(SendStoryRequest(
                                            peer="me",
                                            media=media,
                                            privacy_rules=[InputPrivacyValueAllowAll()],
                                            random_id=random.getrandbits(63),
                                            period=86400,
                                        ))
                                        mark_story_posted_today(acc.id)
                                        logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] 📸 Сториз опубликован ({photo_path.name})")
                                        await _safe_log(db, task_id=log_task_id, account_id=acc.id, action="post_story",
                                                         detail=f"Сториз: {photo_path.name}", success=True,
                                                         source=plan_source, campaign_id=plan_campaign_id)
                                        done_actions += 1
                                    except Exception as e:
                                        err = str(e)
                                        if "PREMIUM" in err.upper() or "PremiumAccountRequired" in err:
                                            logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] 📸 Сториз: не Premium, скип")
                                        else:
                                            logger.warning(f"[plan][{phone}] post_story error: {err[:120]}")
                                except Exception:
                                    pass

                            elif action_type == "reply_dm":
                                try:
                                    dialogs = await client.get_dialogs(limit=20)
                                    users = [d for d in dialogs if getattr(d, 'is_user', False) and d.unread_count > 0]
                                    if users:
                                        u = random.choice(users)
                                        msgs = await client.get_messages(u, limit=5)
                                        for m in msgs:
                                            await client.send_read_acknowledge(u, m)
                                        logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] ↩️ Прочитал ЛС от «{u.name or '?'}»")
                                        await _safe_log(db, task_id=log_task_id, account_id=acc.id, action="reply_dm",
                                                         detail=f"Прочитал ЛС от «{u.name or '?'}»", success=True,
                                                         source=plan_source, campaign_id=plan_campaign_id)
                                        done_actions += 1
                                except Exception:
                                    pass

                            elif action_type == "verify_my_comments":
                                # Проходим по CommentLog акка за окно last N часов
                                # где verified_at IS NULL и коммент старше min_age_sec.
                                # Для каждого:
                                #   1. Резолвим discussion entity через channel_username
                                #      (кешируем per-run — один канал = один resolve).
                                #   2. get_messages(discussion, ids=[msg_id]).
                                #   3. Пусто/None/MessageEmpty → удалён, ChannelBanStats++
                                from models.campaign import CommentLog as _CL
                                from models.channel_ban_stats import ChannelBanStats as _CBS
                                from telethon.tl.functions.channels import GetFullChannelRequest as _GFC
                                from telethon.tl.types import MessageEmpty as _MSE
                                from telethon.errors import FloodWaitError as _FW
                                # Сколько комментов ветки пролистать в поисках своего.
                                # Мало — не долистаем до старого коммента и получим
                                # 'unknown'; много — лишний трафик. 150 покрывает
                                # подавляющее большинство веток.
                                _VERIFY_SCAN = int(os.getenv("VERIFY_SCAN_LIMIT", "150"))
                                window_hours = int(action.get("window_hours", 24))
                                min_age_sec  = int(action.get("min_age_sec", 90))
                                max_checks   = int(action.get("max_checks", 20))
                                _now = datetime.utcnow()
                                _cutoff_age = _now - timedelta(seconds=min_age_sec)
                                _cutoff_win = _now - timedelta(hours=window_hours)
                                # ОДИН verify но ПОЗЖЕ: проверяем только комменты
                                # где verified_at IS NULL И коммент старше 30 мин.
                                # Это гарантирует что проверка произойдёт в
                                # СЛЕДУЮЩЕЙ сессии после отправки (сессии
                                # обычно 30-90 мин apart) — тогда картина уже
                                # финальная, включая медленную модерацию.
                                # Не спамим запросами дважды, один точный ping.
                                _q = await db.execute(
                                    select(_CL).where(
                                        _CL.account_id == acc.id,
                                        _CL.verified_at.is_(None),
                                        _CL.discussion_msg_id.isnot(None),
                                        _CL.created_at < _cutoff_age,
                                        _CL.created_at > _cutoff_win,
                                    ).order_by(_CL.created_at.desc()).limit(max_checks)
                                )
                                _to_check = _q.scalars().all()
                                if not _to_check:
                                    logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] 🔍 verify: непроверенных нет")
                                    done_actions += 1
                                    continue
                                logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] 🔍 verify: {len(_to_check)} комментов")
                                _verified = _deleted_c = 0
                                _flooded = False
                                _disc_cache: dict = {}   # channel_username → discussion entity
                                for _cl in _to_check:
                                    try:
                                        _uname = (_cl.channel_username or "").lstrip("@")
                                        if not _uname:
                                            _cl.verified_at = _now
                                            _cl.verify_status = "not_found"
                                            continue
                                        # ── Читаем ветку ЧЕРЕЗ КОНТЕКСТ ПОСТА ──
                                        # Раньше тут было get_entity(linked_chat_id)
                                        # с последующим get_messages по группе. Оба
                                        # шага ломались (замер 2026-08-25):
                                        #   1. голый числовой id не резолвится, если
                                        #      access_hash группы нет в кэше сессии
                                        #      → ChannelPrivateError;
                                        #   2. даже с верным объектом чтение истории
                                        #      запрещено — аккаунт пишет коммент через
                                        #      contexto поста и участником группы НЕ
                                        #      становится.
                                        # Оба падения ловил общий except и писал
                                        # verify_status='unknown': коммент считался
                                        # проверенным, вердикта не было, статистика
                                        # канала не пополнялась. Так набралось 77
                                        # 'unknown' из 366 проверок.
                                        # iter_messages(канал, reply_to=пост) читает
                                        # ту же ветку без вступления — проверено
                                        # вручную на @mediacomputers и
                                        # @attestatsiyanasiba.
                                        _ch = _disc_cache.get(_uname)
                                        if _ch is None:
                                            _ch = await client.get_entity(_uname)
                                            _disc_cache[_uname] = _ch
                                            await asyncio.sleep(random.uniform(0.8, 2.0))
                                        _my_id = int(_cl.discussion_msg_id)
                                        _seen_ids = []
                                        _m = None
                                        async for _mm in client.iter_messages(
                                            _ch, reply_to=int(_cl.post_id),
                                            limit=_VERIFY_SCAN,
                                        ):
                                            _seen_ids.append(_mm.id)
                                            if _mm.id == _my_id:
                                                _m = _mm
                                                break
                                        if _m is not None and not isinstance(_m, _MSE):
                                            _is_del = False
                                            _undecided = False
                                        elif _seen_ids and min(_seen_ids) <= _my_id <= max(_seen_ids):
                                            # Наш id ВНУТРИ диапазона живых комментов,
                                            # а сообщения нет — значит его удалили.
                                            # Это тот самый различитель, которого не
                                            # хватало в июльском ложном shadow_ban:
                                            # «не нашли» и «удалили» — разные вещи.
                                            _is_del = True
                                            _undecided = False
                                        else:
                                            # Не долистали или ветка пуста — вердикта нет.
                                            _is_del = False
                                            _undecided = True
                                        if _undecided:
                                            _cl.verified_at = datetime.utcnow()
                                            _cl.verify_status = "unknown"
                                            await asyncio.sleep(random.uniform(1.0, 2.5))
                                            continue
                                        _cl.verified_at = datetime.utcnow()
                                        _cl.was_deleted = bool(_is_del)
                                        _cl.verify_status = "deleted" if _is_del else "ok"
                                        # UPSERT ChannelBanStats
                                        _sr = await db.execute(
                                            select(_CBS).where(
                                                _CBS.user_id == campaign.user_id,
                                                _CBS.channel_username == _uname,
                                            )
                                        )
                                        _st = _sr.scalar_one_or_none()
                                        if _st is None:
                                            _st = _CBS(
                                                user_id=campaign.user_id,
                                                channel_username=_uname,
                                            )
                                            db.add(_st)
                                        _st.total_attempts = (_st.total_attempts or 0) + 1
                                        if _is_del:
                                            _st.banned_count = (_st.banned_count or 0) + 1
                                            _st.last_ban_reason = f"auto-verify: msg #{_cl.discussion_msg_id} исчез"
                                            _deleted_c += 1
                                        # IMMEDIATE-WRITE: коммитим прямо тут
                                        # чтобы UI-«Проходимость» и notification
                                        # видели статус НЕ после всей сессии,
                                        # а сразу после каждой проверки.
                                        try:
                                            await db.commit()
                                        except Exception as _ce:
                                            logger.debug(f"[verify] mid-commit fail: {_ce}")
                                            await db.rollback()
                                        # ── ФИКС 2026-08-22 ──────────────────────
                                        # Блок уведомления был ВЛОЖЕН в except выше:
                                        # в норме (коммит прошёл) оно не отправлялось
                                        # никогда, а при сбое коммита — слалось на
                                        # КАЖДЫЙ проверенный коммент, даже целый,
                                        # потому что _is_del не проверялся.
                                        if _is_del:
                                            _notify = getattr(campaign, "notification_chat", None)
                                            if _notify:
                                                try:
                                                    _dsec = int((datetime.utcnow() - _cl.created_at).total_seconds())
                                                    _txt = (
                                                        f"❌ @{_uname} удалили коммент "
                                                        f"акка {_cl.account_phone} через "
                                                        f"{_dsec // 60}м {_dsec % 60}с"
                                                    )
                                                    await client.send_message(_notify, _txt)
                                                except Exception as _ne:
                                                    logger.debug(f"[verify] notify fail: {_ne}")
                                        _verified += 1
                                        await asyncio.sleep(random.uniform(1.5, 4.0))
                                    except _FW as _fw:
                                        logger.warning(f"[verify] FloodWait {_fw.seconds}s — стоп")
                                        _flooded = True
                                        break
                                    except Exception as _e:
                                        _cl.verified_at = datetime.utcnow()
                                        _cl.verify_status = "unknown"
                                        logger.debug(f"[verify] fail cl#{_cl.id}: {type(_e).__name__}: {str(_e)[:100]}")
                                await db.flush()
                                logger.info(
                                    f"[plan][{phone}]   [{action_num}/{len(actions)}] 🔍 verify: "
                                    f"проверено {_verified}, удалено {_deleted_c}"
                                    + (" (flood-stop)" if _flooded else "")
                                )
                                done_actions += 1
                                continue

                            elif action_type == "smart_comment":
                                target_channel = action.get("channel", "")
                                if not target_channel or not campaign:
                                    continue

                                # ── Guard #0: глобальный кэш свойств канала ──
                                # Знание о канале общее для всех аккаунтов
                                # (utils/channel_caps). Если ОДИН аккаунт уже
                                # выяснил, что обсуждений нет / нужна заявка /
                                # писать запрещено — остальные 140 аккаунтов
                                # не должны это переоткрывать и получать отказ
                                # от Telegram по второму кругу. Раньше знание
                                # хранилось per-account, отсюда и накопилось
                                # 156 ключей ch_forbidden и 255 invite_pending.
                                #
                                # Здесь НЕТ запроса в Telegram — только чтение БД.
                                try:
                                    from utils.channel_caps import get_cap as _get_cap
                                    _cap = await _get_cap(db, target_channel)
                                    if _cap is not None and _cap.commentable is False:
                                        logger.info(
                                            f"[plan][{phone}]   [{action_num}/{len(actions)}] "
                                            f"⏭ @{target_channel}: канал непригоден ({_cap.reason}) — "
                                            f"известно заранее, запрос не делаем"
                                        )
                                        await _safe_log(
                                            db, task_id=log_task_id, account_id=acc.id,
                                            action="smart_comment",
                                            detail=f"⏭ skip @{target_channel}: cap_{_cap.reason}",
                                            channel=target_channel, success=False,
                                            error=f"cap_{_cap.reason}"[:64],
                                            error_origin="internal",
                                            source=plan_source, campaign_id=plan_campaign_id,
                                        )
                                        continue
                                except Exception:
                                    pass    # кэш недоступен → работаем как раньше

                                # ── Hard guard #1: не в discussion → НЕ пишем ──
                                # Юзер настоял: пока join_discussion_of не выставил
                                # флаг `disc_joined:{acc}:{ch}`, комментим не должны
                                # даже пытаться. Иначе получим ChatGuestSendForbidden.
                                # НО: если Redis-флага нет — сначала проверяем через
                                # GetParticipantRequest. Если акк реально В discussion
                                # (флаг просто не поставился раньше) — ставим сейчас и
                                # НЕ skip'аем. +1 API запрос, но экономит десятки
                                # smart_comment/день (инцидент 2026-07-16, 123 skip).
                                try:
                                    from utils.redis_pool import get_redis as _get_r_dc
                                    _rdc = _get_r_dc()
                                    _target_clean = (target_channel or "").lstrip("@")
                                    # Проверка invite_pending — если ранее discussion
                                    # ответил InviteRequestSent, нам никогда не попасть.
                                    # Per-acc-channel forbidden (chat_write_forbidden / user_banned /
                                    # ChannelPrivateError уже ранее). Skip рано без запросов.
                                    _forbid_key = f"gramgpt:ch_forbidden:{acc.id}:{_target_clean}"
                                    if _rdc.get(_forbid_key):
                                        # Не теряем слот: берём другой канал из
                                        # назначенных этому аккаунту.
                                        _repl = await _pick_replacement_channel(
                                            db, acc.id, plan_campaign_id, _target_clean, _rdc)
                                        if _repl:
                                            logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] 🔄 @{_target_clean} закрыт — пишем в @{_repl}")
                                            target_channel = _repl
                                            _target_clean = _repl
                                            # entity ниже пересоздаётся из target_channel
                                            entity = None
                                        else:
                                            logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] ⏭ @{_target_clean}: акк тут забанен, замены нет — skip")
                                            await _safe_log(db, task_id=log_task_id, account_id=acc.id, action="smart_comment",
                                                             detail=f"⏭ skip @{_target_clean}: acc_forbidden_cached",
                                                             channel=_target_clean, success=False, error="acc_forbidden_cached",
                                                             source=plan_source, campaign_id=plan_campaign_id)
                                            continue
                                    _inv_key = f"gramgpt:disc_invite_pending:{acc.id}:{_target_clean}"
                                    if _rdc.get(_inv_key):
                                        logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] ⏭ @{_target_clean}: invite_pending — skip")
                                        await _safe_log(db, task_id=log_task_id, account_id=acc.id, action="smart_comment",
                                                         detail=f"⏭ skip @{_target_clean}: invite_pending",
                                                         channel=_target_clean, success=False, error="invite_pending",
                                                         source=plan_source, campaign_id=plan_campaign_id)
                                        # Канал закрыт заявкой у ЭТОГО акка — вероятно у всех
                                        # то же самое (closed discussion). Помечаем канал как
                                        # без-комментов, чтобы при re-parse не попадал снова.
                                        await _blacklist_bad_channel(db, _target_clean, reason="invite_pending")
                                        continue
                                    _dj_flag = _rdc.get(f"gramgpt:disc_joined:{acc.id}:{_target_clean}")
                                    if not _dj_flag:
                                        # ── СМЯГЧЁННЫЙ hard_guard (2026-07-30) ──
                                        # Раньше тут делали GetEntity(linked_id) → GetParticipant,
                                        # но get_entity падал ChannelPrivateError на приватных
                                        # discussion → 703 ложных skip'а в camp#54/55. Guard
                                        # смягчили: пусть основной smart_comment попробует, а
                                        # _CGSFE handler (auto-join через disc.chats) вступит
                                        # и повторит сам.
                                        #
                                        # ПОЧИНЕНО 2026-08-27. При том смягчении внизу блока
                                        # остался `continue` от удалённой ветки — и он
                                        # выполнялся ВСЕГДА. То есть коммент в канал, где у
                                        # аккаунта ещё нет флага disc_joined, молча
                                        # выбрасывался: ни строки в warmup_logs (UI показывал
                                        # «не было ошибок»), зато каналу капал skipped_count с
                                        # причиной hard_guard_no_discussion. За 27.08 так
                                        # потерялось 103 коммента из 159 запланированных.
                                        # Здесь не должно быть ни continue, ни записи skip:
                                        # это не пропуск, а разрешение идти дальше.
                                        logger.info(
                                            f"[plan][{phone}]   [{action_num}/{len(actions)}] "
                                            f"🔓 @{_target_clean}: hard_guard soft-pass "
                                            f"(auto-join разберётся)"
                                        )
                                except Exception:
                                    pass    # Redis сбой → fallback на старую логику

                                # Guard: комментируем только в joined каналах.
                                # Применяется только для кампаний с assignment-записями
                                # (т.е. запущенных после migrate 024).
                                # Если таблица не создана — guard не мешает (обратная совместимость).
                                _allow_comment = True
                                if plan.campaign_id:
                                    try:
                                        from models.campaign_channel_assignment import CampaignChannelAssignment
                                        # begin_nested() = SAVEPOINT: если запрос упадёт (таблица не создана),
                                        # откатится только savepoint, внешняя транзакция не ломается.
                                        async with db.begin_nested():
                                            has_asgn = (await db.execute(
                                                select(CampaignChannelAssignment.id).where(
                                                    CampaignChannelAssignment.campaign_id == plan.campaign_id
                                                ).limit(1)
                                            )).scalar_one_or_none()
                                            if has_asgn is not None:
                                                # Кампания с organic funnel — применяем guard
                                                sub = (await db.execute(
                                                    select(CampaignChannelAssignment.id).where(
                                                        CampaignChannelAssignment.campaign_id      == plan.campaign_id,
                                                        CampaignChannelAssignment.account_id       == acc.id,
                                                        CampaignChannelAssignment.channel_username == target_channel,
                                                        CampaignChannelAssignment.status           == "joined",
                                                    )
                                                )).scalar_one_or_none()
                                                _allow_comment = (sub is not None)
                                    except Exception:
                                        pass  # Таблица не создана → старое поведение (разрешаем)

                                if not _allow_comment:
                                    # Auto-join fallback: если это check_only-кампания
                                    # (или organic funnel не сработал по каналу) —
                                    # пробуем вступить прямо сейчас, потом коммент.
                                    _camp_type = getattr(campaign, "campaign_type", "regular") if campaign else "regular"
                                    _auto_joined = False
                                    try:
                                        from telethon.tl.functions.channels import (
                                            JoinChannelRequest as _JCR2, GetParticipantRequest as _GPR2,
                                        )
                                        from telethon.errors import UserAlreadyParticipantError as _UAPE
                                        _ent = await client.get_entity(target_channel)
                                        _me = await client.get_me()
                                        try:
                                            await client(_GPR2(channel=_ent, participant=_me))
                                            _auto_joined = True   # уже в канале
                                        except Exception:
                                            try:
                                                await client(_JCR2(_ent))
                                                _auto_joined = True
                                            except _UAPE:
                                                _auto_joined = True
                                        if _auto_joined and plan.campaign_id:
                                            from models.campaign_channel_assignment import CampaignChannelAssignment
                                            _ca = (await db.execute(
                                                select(CampaignChannelAssignment).where(
                                                    CampaignChannelAssignment.campaign_id == plan.campaign_id,
                                                    CampaignChannelAssignment.account_id  == acc.id,
                                                    CampaignChannelAssignment.channel_username == target_channel,
                                                )
                                            )).scalar_one_or_none()
                                            if _ca:
                                                _ca.status = "joined"
                                                _ca.joined_at = datetime.utcnow()
                                            else:
                                                db.add(CampaignChannelAssignment(
                                                    campaign_id=plan.campaign_id,
                                                    account_id=acc.id,
                                                    channel_username=target_channel,
                                                    status="joined",
                                                    joined_at=datetime.utcnow(),
                                                ))
                                            await db.flush()
                                            await db.commit()
                                            _allow_comment = True
                                            logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] 🔗 auto-join перед комментом @{target_channel}")
                                            await asyncio.sleep(random.uniform(3, 7))
                                    except Exception as _je:
                                        logger.warning(f"[plan][{phone}]   [{action_num}/{len(actions)}] auto-join fail @{target_channel}: {type(_je).__name__}: {str(_je)[:100]}")

                                if not _allow_comment:
                                    logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] ⏭ @{target_channel}: ещё не подписан — пропуск")
                                    await _safe_log(db, task_id=log_task_id, account_id=acc.id, action="smart_comment",
                                                     detail=f"⏭ skip @{target_channel}: not_subscribed",
                                                     channel=target_channel, success=False, error="not_subscribed",
                                                     source=plan_source, campaign_id=plan_campaign_id)
                                    try:
                                        from models.channel_ban_stats import ChannelBanStats as _CBS_ns
                                        _st_ns = (await db.execute(
                                            select(_CBS_ns).where(
                                                _CBS_ns.user_id == campaign.user_id,
                                                _CBS_ns.channel_username == target_channel,
                                            )
                                        )).scalar_one_or_none()
                                        if not _st_ns:
                                            _st_ns = _CBS_ns(
                                                user_id=campaign.user_id,
                                                channel_username=target_channel,
                                                total_attempts=0, banned_count=0,
                                                skipped_count=0,
                                            )
                                            db.add(_st_ns)
                                        _st_ns.total_attempts = (_st_ns.total_attempts or 0) + 1
                                        _st_ns.skipped_count = (_st_ns.skipped_count or 0) + 1
                                        _st_ns.last_skip_reason = "not_subscribed"
                                        _st_ns.last_updated = datetime.utcnow()
                                        await db.flush()
                                    except Exception:
                                        pass
                                    continue

                                try:
                                    entity = await client.get_entity(target_channel)
                                    # Тянем 10-15 постов чтобы гарантированно найти
                                    # пост с уже созданным discussion-thread
                                    posts = await client.get_messages(entity, limit=random.randint(10, 15))
                                    for p in posts:
                                        await client.send_read_acknowledge(entity, p)
                                        await asyncio.sleep(random.uniform(0.5, 3))

                                    # ── КРИТИЧНО: фильтруем посты БЕЗ комментов ──
                                    # TG НЕ СОЗДАЁТ discussion-thread пока никто не
                                    # написал коммент → GetDiscussionMessageRequest
                                    # падает с MsgIdInvalidError. Проверено вживую
                                    # 2026-07-18 на @khutoryanka515: посты с
                                    # replies=0 давали MsgIdInvalid, replies>0 работали.
                                    # Пропускаем посты без комментов — на них
                                    # физически нельзя написать через API.
                                    _all_posts_count = len(posts)
                                    posts = [
                                        p for p in posts
                                        if getattr(p, "replies", None) is not None
                                        and (p.replies.replies or 0) > 0
                                    ]
                                    if _all_posts_count and not posts:
                                        logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] ⏭ @{target_channel}: у всех {_all_posts_count} свежих постов replies=0 (thread ещё не создан)")

                                    # ── Фильтруем "плохие посты" (MsgIdInvalid ранее) через Redis ──
                                    # Плохие посты помечены с TTL 1ч: gramgpt:bad_post:{channel}:{post_id}
                                    # Это shared между всеми акками — если у одного упало на посте,
                                    # другой не тыкает его в течение часа.
                                    try:
                                        from utils.redis_pool import get_redis as _get_r_bp
                                        _r_bp = _get_r_bp()
                                        _clean_ch_bp = target_channel.lstrip('@')
                                        posts = [p for p in posts if not _r_bp.get(f"gramgpt:bad_post:{_clean_ch_bp}:{p.id}")]
                                    except Exception:
                                        pass

                                    if not posts:
                                        # Silent skip: канал без постов. Записываем
                                        # в channel_ban_stats.skipped_count чтобы
                                        # проходимость показала «мёртвый канал».
                                        logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] ⏭ @{target_channel}: skip (no_posts)")
                                        await _safe_log(db, task_id=log_task_id, account_id=acc.id, action="smart_comment",
                                                         detail=f"⏭ skip @{target_channel}: no_posts",
                                                         channel=target_channel, success=False, error="no_posts",
                                                         source=plan_source, campaign_id=plan_campaign_id)
                                        try:
                                            from models.channel_ban_stats import ChannelBanStats as _CBS_np
                                            _st_np = (await db.execute(
                                                select(_CBS_np).where(
                                                    _CBS_np.user_id == campaign.user_id,
                                                    _CBS_np.channel_username == target_channel,
                                                )
                                            )).scalar_one_or_none()
                                            if not _st_np:
                                                _st_np = _CBS_np(
                                                    user_id=campaign.user_id,
                                                    channel_username=target_channel,
                                                    total_attempts=0, banned_count=0,
                                                    skipped_count=0,
                                                )
                                                db.add(_st_np)
                                            _st_np.total_attempts = (_st_np.total_attempts or 0) + 1
                                            _st_np.skipped_count = (_st_np.skipped_count or 0) + 1
                                            _st_np.last_skip_reason = "no_posts"
                                            _st_np.last_updated = datetime.utcnow()
                                            await db.flush()
                                        except Exception:
                                            pass
                                        continue

                                    # ── Берём свежий пост, У КОТОРОГО ЕСТЬ ТЕКСТ ──
                                    # Раньше брали строго posts[0]. Если верхний
                                    # пост — фото/видео без подписи, в LLM уходило
                                    # post_text="", модель отвечала «дайте текст
                                    # поста», и эта фраза публиковалась в канал
                                    # как комментарий. Замер 2026-08-24 по
                                    # comment_logs: 95 из 1004 (9.5%) комментов
                                    # написаны к пустому посту, 39 из них —
                                    # опубликованная отмазка модели.
                                    # Сужаем сам список `posts`: он же используется
                                    # ниже как fallback при MsgIdInvalid, так что и
                                    # там теперь не попадётся пост без текста.
                                    from services.llm import MIN_POST_CHARS as _MPC
                                    _texted = [_p for _p in posts
                                               if len((_p.message or "").strip()) >= _MPC]
                                    if not _texted:
                                        logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] ⏭ @{target_channel}: skip (no_text_posts)")
                                        await _safe_log(db, task_id=log_task_id, account_id=acc.id, action="smart_comment",
                                                         detail=f"⏭ skip @{target_channel}: no_text_posts",
                                                         channel=target_channel, success=False,
                                                         error="no_text_posts", error_origin="internal",
                                                         source=plan_source, campaign_id=plan_campaign_id)
                                        done_actions += 1
                                        continue
                                    if len(_texted) < len(posts):
                                        logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] 📝 @{target_channel}: {len(posts)-len(_texted)} постов без текста пропущено")
                                    posts = _texted
                                    target_post = posts[0]
                                    post_text = target_post.message or ""

                                    typing_dur = action.get("pause_before", random.randint(2, 20))
                                    logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] ⌨️ Typing {typing_dur}с в @{target_channel}")
                                    try:
                                        from telethon.tl.functions.messages import SetTypingRequest, GetDiscussionMessageRequest
                                        from telethon.tl.types import SendMessageTypingAction
                                        disc = await client(GetDiscussionMessageRequest(peer=entity, msg_id=target_post.id))
                                        if disc and disc.messages:
                                            await client(SetTypingRequest(peer=disc.messages[0].peer_id, action=SendMessageTypingAction()))
                                    except Exception:
                                        pass
                                    await asyncio.sleep(typing_dur)

                                    # 2026-07-23 — снизил "передумал" с 5-15% до 1-3%
                                    # и НЕ пишем как success (это раздувало WarmupLog.ok):
                                    # было 9 из 15 = 60% "успехов" на самом деле передумал.
                                    if random.random() < random.uniform(0.01, 0.03):
                                        logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] 🚫 Передумал комментировать @{target_channel}")
                                        await _safe_log(db, task_id=log_task_id, account_id=acc.id, action="smart_comment",
                                                         detail=f"🚫 Передумал комментировать @{target_channel}",
                                                         channel=target_channel, success=False, error="user_declined",
                                                         source=plan_source, campaign_id=plan_campaign_id)
                                        done_actions += 1
                                        continue

                                    style_profile = assign_style_profile(phone)
                                    prompt = build_comment_prompt(post_text, style_profile, plan.plan)
                                    provider = plan.plan.get("personality_data", {}).get("llm_provider", "groq")
                                    if campaign:
                                        provider = _val(campaign.llm_provider)

                                    # Берём API ключ(и) из БД.
                                    # Для Groq — весь пул активных ключей юзера
                                    # (ротация при 429). Для остальных — один
                                    # default-ключ (можно потом расширить).
                                    from services.service_credentials import get_api_key, get_api_keys_pool
                                    api_key = None
                                    api_keys = None
                                    if campaign:
                                        if provider == "groq":
                                            api_keys = await get_api_keys_pool(db, campaign.user_id, "groq")
                                        else:
                                            api_key = await get_api_key(
                                                db, campaign.user_id, provider,
                                                credential_id=getattr(campaign, "llm_credential_id", None),
                                            )

                                    # generate_comment_ex отдаёт ПРИЧИНУ отказа.
                                    # Раньше звали generate_comment и писали в лог
                                    # одно слово `llm_empty` на любой сбой — из-за
                                    # этого июльский инцидент (кончилась квота
                                    # единственного ключа) и августовский (Groq снял
                                    # модель, 404) выглядели в статистике одинаково.
                                    from services.llm import generate_comment_ex
                                    comment_text, _llm_reason = generate_comment_ex(
                                        provider, prompt, post_text,
                                        api_key=api_key, api_keys=api_keys,
                                    )
                                    if not comment_text:
                                        logger.warning(
                                            f"[plan][{phone}]   [{action_num}/{len(actions)}] "
                                            f"❌ LLM не дал текст: {_llm_reason}"
                                        )
                                        # error_origin='internal': это НАША проблема,
                                        # аккаунт не виноват и отдыхать не должен.
                                        await _safe_log(db, task_id=log_task_id, account_id=acc.id, action="smart_comment",
                                                         detail=f"⏭ skip @{target_channel}: {_llm_reason}",
                                                         channel=target_channel, success=False,
                                                         error=_llm_reason, error_origin="internal",
                                                         source=plan_source, campaign_id=plan_campaign_id)
                                        continue

                                    # Auto-join discussion при ChatGuestSendForbidden.
                                    # Канал ≠ его discussion group — надо явно вступить
                                    # в discussion. Из-за этого 10%+ комментов раньше
                                    # падали с "You join the discussion group before
                                    # commenting" и просто скипались.
                                    from telethon.errors import (
                                        ChatGuestSendForbiddenError as _CGSFE,
                                        ChatWriteForbiddenError as _CWFE3,
                                        UserBannedInChannelError as _UBICE3,
                                        UserAlreadyParticipantError as _UAPE3,
                                        MsgIdInvalidError as _MIIE3,
                                    )
                                    from telethon.tl.functions.channels import (
                                        GetFullChannelRequest as _GFCR3,
                                        JoinChannelRequest as _JCR3,
                                    )
                                    # 3 попытки: (1) прямая, (2) после join
                                    # discussion, (3) после свежего resolve
                                    # (для MsgIdInvalidError — глюк кэша
                                    # discussion linkage у Telethon).
                                    _sent_msg = None
                                    _skip_reason = None
                                    for _cmt_try in range(3):
                                        try:
                                            _sent_msg = await client.send_message(
                                                entity=entity, message=comment_text,
                                                comment_to=target_post.id,
                                            )
                                            break
                                        except _CGSFE:
                                            if _cmt_try >= 2:
                                                # После 3 попыток всё ещё нельзя — либо
                                                # discussion закрыт, либо канал без него.
                                                # Не raise → не считать за ошибку.
                                                _skip_reason = "no_discussion_access"
                                                break
                                            # Резолвим discussion и вступаем.
                                            # ВАЖНО: get_entity(link_id) часто фейлится
                                            # ChannelPrivateError т.к. discussion group
                                            # приватна для non-participants. Правильный
                                            # способ — взять chat obj из ответа
                                            # GetDiscussionMessageRequest (там есть
                                            # access_hash полученный через post-context).
                                            try:
                                                _fc = await client(_GFCR3(entity))
                                                _link_id = getattr(_fc.full_chat, "linked_chat_id", 0) or 0
                                                if not _link_id:
                                                    # Канал без discussion group — silent skip
                                                    _skip_reason = "no_linked_chat"
                                                    break
                                                # ── Получаем disc chat obj через GetDiscussionMessage ──
                                                _disc_e = None
                                                try:
                                                    from telethon.tl.functions.messages import GetDiscussionMessageRequest as _GDMR3
                                                    _dmsg_r = await client(_GDMR3(peer=entity, msg_id=target_post.id))
                                                    for _ch_obj in (_dmsg_r.chats or []):
                                                        if _ch_obj.id == _link_id:
                                                            _disc_e = _ch_obj
                                                            break
                                                except Exception:
                                                    pass
                                                # Fallback: get_entity если через disc.chats не нашли
                                                if _disc_e is None:
                                                    _disc_e = await client.get_entity(_link_id)
                                                try:
                                                    await client(_JCR3(_disc_e))
                                                except _UAPE3:
                                                    pass
                                                # ── СТАВИМ Redis-флаг чтобы hard guard
                                                # знал: этот акк теперь в discussion.
                                                # Иначе следующий smart_comment этого
                                                # канала снова пойдёт по auto-join.
                                                try:
                                                    from utils.redis_pool import get_redis as _get_r_aj
                                                    _get_r_aj().setex(
                                                        f"gramgpt:disc_joined:{acc.id}:{target_channel.lstrip('@')}",
                                                        30 * 86400, "1",
                                                    )
                                                except Exception:
                                                    pass
                                                # Глобальный реестр подписок
                                                try:
                                                    from utils.subscriptions import record_discussion_join
                                                    await record_discussion_join(db, acc.id, target_channel, via="auto_join")
                                                except Exception:
                                                    pass
                                                logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] 🔗 auto-join discussion @{target_channel}")
                                                await asyncio.sleep(random.uniform(2, 5))
                                            except Exception as _je_aj:
                                                # Не смогли join discussion — silent skip
                                                _skip_reason = f"auto_join_fail: {type(_je_aj).__name__}"
                                                break
                                        except _CWFE3:
                                            # Discussion group закрыт для юзеров (только
                                            # админы) ИЛИ акк заблокирован. Retry не поможет.
                                            _skip_reason = "chat_write_forbidden"
                                            # Помечаем per-acc-channel на 30 дней чтоб этот
                                            # акк больше не тыкал этот канал (сэкономит запросы).
                                            try:
                                                from utils.redis_pool import get_redis as _get_r_cwf
                                                _get_r_cwf().setex(
                                                    f"gramgpt:ch_forbidden:{acc.id}:{target_channel.lstrip('@')}",
                                                    30 * 86400, "1",
                                                )
                                            except Exception:
                                                pass
                                            break
                                        except _UBICE3:
                                            # Акк ban'нут в этом чате — навсегда.
                                            # Помечаем в ChannelBanStats + per-acc Redis.
                                            _skip_reason = "user_banned"
                                            try:
                                                from utils.redis_pool import get_redis as _get_r_ub
                                                _get_r_ub().setex(
                                                    f"gramgpt:ch_forbidden:{acc.id}:{target_channel.lstrip('@')}",
                                                    30 * 86400, "1",
                                                )
                                            except Exception:
                                                pass
                                            try:
                                                from models.channel_ban_stats import ChannelBanStats
                                                _st_b = (await db.execute(
                                                    select(ChannelBanStats).where(
                                                        ChannelBanStats.user_id == campaign.user_id,
                                                        ChannelBanStats.channel_username == target_channel.lstrip('@'),
                                                    )
                                                )).scalar_one_or_none()
                                                if _st_b:
                                                    _st_b.banned_count = (_st_b.banned_count or 0) + 1
                                                    _st_b.total_attempts = (_st_b.total_attempts or 0) + 1
                                                    _st_b.last_ban_reason = "UserBannedInChannel"
                                                    await db.flush()
                                            except Exception:
                                                pass
                                            break
                                        except _MIIE3:
                                            # TG-глюк: MsgIdInvalidError на
                                            # GetDiscussionMessageRequest. Обычно =
                                            # пост удалён между чтением и попыткой.
                                            # Помечаем ЭТОТ пост как "плохой" в Redis
                                            # на 1ч (shared между всеми акками — если
                                            # у одного упало, другие не тыкают) и
                                            # переключаемся на СЛЕДУЮЩИЙ пост из
                                            # списка `posts` (уже загружен, кэш свежий).
                                            try:
                                                from utils.redis_pool import get_redis as _get_r_mi
                                                _get_r_mi().setex(
                                                    f"gramgpt:bad_post:{target_channel.lstrip('@')}:{target_post.id}",
                                                    3600, "1",
                                                )
                                            except Exception:
                                                pass
                                            # Ищем следующий не-текущий пост в списке
                                            _next_post = None
                                            for _p in posts:
                                                if _p.id != target_post.id:
                                                    _next_post = _p
                                                    break
                                            if _next_post is None or _cmt_try >= 2:
                                                # Постов больше нет, или уже 3 попытки —
                                                # silent skip (пост удалён / кэш глюк).
                                                _skip_reason = "msg_id_invalid_persistent"
                                                break
                                            logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] 🔄 MsgIdInvalid: пост {target_post.id} → пробуем {_next_post.id} @{target_channel}")
                                            # Удаляем текущий пост из списка, переключаемся
                                            posts = [_p for _p in posts if _p.id != target_post.id]
                                            target_post = _next_post
                                            post_text = target_post.message or ""
                                            await asyncio.sleep(random.uniform(2, 4))
                                        except Exception as _generic_send_err:
                                            # Ловим прочие RPCError'ы которые нет смысла retry-ать:
                                            #   - ALLOW_PAYMENT_REQUIRED_XXX — платный канал (Stars)
                                            #   - channel private / lack permission — кикнут админом
                                            #   - You cannot send plain results — inline-only чат
                                            # Все эти "постоянные" ошибки канала → silent skip.
                                            _err_msg = str(_generic_send_err).lower()
                                            _err_type = type(_generic_send_err).__name__
                                            _permanent_channel_errors = (
                                                "allow_payment_required",       # Stars-only channel
                                                "channel_private",               # приватный / кикнут
                                                "channel specified is private",  # то же, но текст
                                                "lack permission",
                                                "cannot send plain results",     # inline-only
                                                "chat_send_media_forbidden",     # запрет медиа
                                                "peer_id_invalid",
                                                "usernameinvalid",
                                                "usernamenotoccupied",
                                            )
                                            if any(p in _err_msg for p in _permanent_channel_errors):
                                                _skip_reason = f"channel_bad: {_err_type}"
                                                # Помечаем per-acc-channel forbidden в Redis
                                                # чтобы этот акк больше не тыкал этот канал (30 дней)
                                                try:
                                                    from utils.redis_pool import get_redis as _get_r_pce
                                                    _get_r_pce().setex(
                                                        f"gramgpt:ch_forbidden:{acc.id}:{target_channel.lstrip('@')}",
                                                        30 * 86400, "1",
                                                    )
                                                except Exception:
                                                    pass
                                                break
                                            # Всё остальное — реальная ошибка → raise дальше
                                            # (upstream handler запишет как error)
                                            raise

                                    # ── POST-SEND VERIFY: подтверждаем что коммент реально виден в discussion ──
                                    # Инцидент 2026-07-19: акки в shadow ban → send_message возвращает
                                    # success + Message-объект, но реально сообщение в чат не попадает.
                                    # Из 51 CommentLog в БД реально доходило ~10-15 (проверено вручную).
                                    # Проверяем через iter_messages в discussion + флаг acc.
                                    if _sent_msg is not None:
                                        try:
                                            # ── ФИКС 2026-08-22 ─────────────────────────
                                            # Раньше тут было:
                                            #   iter_messages(disc, reply_to=target_post.id)
                                            # где target_post.id — id поста В КАНАЛЕ. Но для
                                            # GetRepliesRequest по peer=discussion нужен id
                                            # топ-сообщения В ГРУППЕ ОБСУЖДЕНИЙ — это ДРУГОЕ
                                            # число. В результате Telegram либо отвечал
                                            # MsgIdInvalidError, либо (хуже) отдавал ЧУЖОЙ тред,
                                            # где нашего коммента естественно нет → код объявлял
                                            # shadow_ban на успешно доставленном комменте.
                                            # Замер вживую 2026-08-22 (acc#207, 5 каналов):
                                            # 5/5 комментов реально доставлены, прод засчитал 0.
                                            # Цена ошибки: коммент не попадает в CommentLog,
                                            # camp.comments_sent не растёт, акку +1 страйк
                                            # (2 = frozen), каналу → blacklist.
                                            #
                                            # Теперь проверяем так же, как это делает отложенный
                                            # verify_my_comments — выборкой по ЯВНОМУ id:
                                            #   get_messages(disc, ids=[_sent_msg.id])
                                            # Этот способ подтверждён: 90.3% ok в comment_logs
                                            # и 5/5 в живом тесте.
                                            from telethon.tl.types import MessageEmpty as _MSE_v
                                            _chat_v = None
                                            try:
                                                # Пир самого отправленного сообщения — самый
                                                # надёжный источник (access_hash уже в кэше).
                                                _chat_v = await _sent_msg.get_input_chat()
                                            except Exception:
                                                _chat_v = None
                                            if _chat_v is None:
                                                from telethon.tl.functions.channels import GetFullChannelRequest as _GFC_v
                                                _full_v = await client(_GFC_v(entity))
                                                _link_v = getattr(_full_v.full_chat, "linked_chat_id", 0) or 0
                                                if _link_v:
                                                    _chat_v = await client.get_entity(_link_v)
                                            if _chat_v is not None:
                                                await asyncio.sleep(random.uniform(2, 5))
                                                _chk = await client.get_messages(_chat_v, ids=[int(_sent_msg.id)])
                                                _m_chk = _chk[0] if _chk else None
                                                # ЯВНОЕ отсутствие = сообщение не дошло/снесено
                                                # мгновенно. Всё остальное считаем успехом.
                                                _verified_visible = not (
                                                    _m_chk is None
                                                    or isinstance(_m_chk, _MSE_v)
                                                    or (getattr(_m_chk, "message", None) is None
                                                        and getattr(_m_chk, "media", None) is None)
                                                )
                                                if not _verified_visible:
                                                    # Shadow-ban или blocked
                                                    logger.warning(f"[plan][{phone}]   [{action_num}/{len(actions)}] 🔇 SHADOW BAN? send OK но msg НЕ в discussion @{target_channel}")
                                                    _sent_msg = None
                                                    _skip_reason = "shadow_ban_no_visible_msg"
                                                    # 2026-07-23 — снизил порог 3→2, TTL 6ч→48ч
                                                    try:
                                                        from utils.redis_pool import get_redis as _get_r_sb
                                                        from models.account import AccountStatus as _ASt
                                                        _r_sb = _get_r_sb()
                                                        # ── Считаем РАЗНЫЕ каналы, а не срабатывания ──
                                                        # Раньше здесь был простой счётчик: два попадания
                                                        # за 48 часов — и аккаунт в заморозку. Но два
                                                        # попадания в ОДНОМ канале означают, что канал
                                                        # мгновенно съедает комментарии, а не что аккаунт
                                                        # теневой. Замер 26.08: acc#181 заморожен по двум
                                                        # срабатываниям, оба в @konkor1403i, при этом его
                                                        # коммент в @eafc_prime проверен и жив.
                                                        # Теневой бан — свойство аккаунта, он проявляется
                                                        # везде. Поэтому считаем разные каналы, как и в
                                                        # правиле про персональные отказы.
                                                        _sb_key = f"gramgpt:shadow_ban:{acc.id}"
                                                        _r_sb.sadd(_sb_key, target_channel.lstrip("@").lower())
                                                        _sb_cnt = int(_r_sb.scard(_sb_key) or 0)
                                                        _r_sb.expire(_sb_key, 48 * 3600)
                                                        # ── Порог поднят до 3 каналов, и заморозки больше нет ──
                                                        # `shadow_ban_no_visible_msg` значит лишь «отправили, но
                                                        # при перечитывании не увидели». Так выглядит и медленная
                                                        # доставка, и модерация одного коммента, и наш неудачный
                                                        # обход треда. Двух срабатываний для приговора мало:
                                                        # acc#181 замораживали дважды (26.08 и 30.08), и оба раза
                                                        # он в той же сессии писал комменты, которые проходили.
                                                        #
                                                        # Теперь: три разных канала, и перед приговором смотрим,
                                                        # есть ли у аккаунта коммент, ПОДТВЕРЖДЁННО живой за
                                                        # последние двое суток. Есть — значит теневого бана нет,
                                                        # счётчик сбрасываем. Нет — отправляем отдыхать, а не
                                                        # замораживаем: отдых рассасывается сам, а frozen
                                                        # приходится снимать руками.
                                                        if int(_sb_cnt) >= 3:
                                                            # Хватает факта «есть хотя бы один живой» —
                                                            # считать все ни к чему, а select(...).limit(1)
                                                            # не зависит от того, импортирован ли func
                                                            # в этой области видимости.
                                                            _alive_recent = 0
                                                            try:
                                                                from models.campaign import CommentLog as _CL_sb
                                                                _alive_recent = (await db.execute(
                                                                    select(_CL_sb.id).where(
                                                                        _CL_sb.account_id == acc.id,
                                                                        _CL_sb.verify_status == "ok",
                                                                        _CL_sb.created_at >= datetime.utcnow() - timedelta(hours=48),
                                                                    ).limit(1))).scalar_one_or_none()
                                                            except Exception:
                                                                _alive_recent = 0
                                                            if _alive_recent:
                                                                logger.info(
                                                                    f"[plan][{phone}] shadow-ban подозрение снято: "
                                                                    f"есть подтверждённо живой коммент за 48ч"
                                                                )
                                                                _r_sb.delete(_sb_key)
                                                            else:
                                                                logger.error(
                                                                    f"[plan][{phone}] 🚨 acc#{acc.id} похоже на shadow ban "
                                                                    f"({_sb_cnt} разных каналов, живых комментов нет) — отдых 24ч"
                                                                )
                                                                from utils.account_health import set_rest as _set_rest_sb
                                                                _set_rest_sb(acc.id, 24 * 3600,
                                                                             f"подозрение на shadow ban ({_sb_cnt} каналов)")
                                                        # Per-channel: если 2+ РАЗНЫХ акков поймали shadow-ban в
                                                        # одном канале за 48ч — канал считаем «мёртвым для комментов»
                                                        # и blacklist'им (не убираем, но пометим как no-comments).
                                                        _ch_sb_key = f"gramgpt:shadow_ban_ch:{target_channel}"
                                                        _r_sb.sadd(_ch_sb_key, acc.id)
                                                        _r_sb.expire(_ch_sb_key, 48 * 3600)
                                                        _ch_acc_cnt = _r_sb.scard(_ch_sb_key)
                                                        if int(_ch_acc_cnt) >= 2:
                                                            logger.error(f"[plan] 📵 канал @{target_channel} → shadow-ban от {_ch_acc_cnt} акков — blacklist")
                                                            await _blacklist_bad_channel(db, target_channel, reason="shadow_ban_channel")
                                                    except Exception as _sb_e:
                                                        logger.warning(f"[plan] shadow_ban mark err: {_sb_e}")
                                                else:
                                                    # Успешный verify → сбрасываем shadow-ban счётчик
                                                    try:
                                                        from utils.redis_pool import get_redis as _get_r_sb2
                                                        _get_r_sb2().delete(f"gramgpt:shadow_ban:{acc.id}")
                                                    except Exception:
                                                        pass
                                        except Exception as _v_err:
                                            logger.debug(f"[plan][{phone}] post-send verify err: {_v_err}")

                                    # Если ничего не отправили — silent skip без ошибки.
                                    # Пишем в ChannelBanStats.skipped_count чтобы в
                                    # «Проходимости» было видно системные проблемы канала.
                                    if _sent_msg is None:
                                        logger.info(
                                            f"[plan][{phone}]   [{action_num}/{len(actions)}] "
                                            f"⏭ @{target_channel}: skip ({_skip_reason or 'unknown'})"
                                        )
                                        # ── Пассивное обучение кэша (2026-08-22) ──
                                        # Отказ уже случился — записываем его как СВОЙСТВО
                                        # КАНАЛА, чтобы остальные 140 аккаунтов сюда не
                                        # ходили. Ноль дополнительных запросов к Telegram.
                                        #
                                        # Решаем классификатором, а не списком строк:
                                        # причины бывают составные ("channel_bad:
                                        # ChannelPrivateError"), и жёсткий словарь их
                                        # пропускал — поймано E2E-тестом 2026-08-22.
                                        # Пишем только severity='channel': shadow_ban и
                                        # наши internal-коды сюда попадать не должны,
                                        # это свойства пары (аккаунт, канал), а не канала.
                                        try:
                                            from utils.error_origin import classify_severity as _sev_sk
                                            from utils.channel_caps import mark_bad as _cap_bad
                                            _nice = {
                                                "chat_write_forbidden": "write_banned",
                                                "no_linked_chat":       "no_discussion",
                                                "no_discussion_access": "no_discussion",
                                                "user_banned":          "user_banned",
                                            }
                                            if _skip_reason and _sev_sk(_skip_reason, None) == "channel":
                                                await _cap_bad(
                                                    db, target_channel,
                                                    reason=_nice.get(_skip_reason, _skip_reason)[:64],
                                                    account_id=acc.id, error=_skip_reason,
                                                )
                                        except Exception:
                                            pass
                                        await _safe_log(db, task_id=log_task_id, account_id=acc.id, action="smart_comment",
                                                         detail=f"⏭ skip @{target_channel}: {_skip_reason or 'unknown'}",
                                                         channel=target_channel, success=False, error=_skip_reason or "unknown",
                                                         source=plan_source, campaign_id=plan_campaign_id)
                                        try:
                                            from models.channel_ban_stats import ChannelBanStats as _CBS_sk
                                            _st_sk = (await db.execute(
                                                select(_CBS_sk).where(
                                                    _CBS_sk.user_id == campaign.user_id,
                                                    _CBS_sk.channel_username == target_channel,
                                                )
                                            )).scalar_one_or_none()
                                            if not _st_sk:
                                                _st_sk = _CBS_sk(
                                                    user_id=campaign.user_id,
                                                    channel_username=target_channel,
                                                    total_attempts=0, banned_count=0,
                                                    skipped_count=0,
                                                )
                                                db.add(_st_sk)
                                            _st_sk.total_attempts = (_st_sk.total_attempts or 0) + 1
                                            _st_sk.skipped_count = (_st_sk.skipped_count or 0) + 1
                                            _st_sk.last_skip_reason = _skip_reason or "unknown"
                                            _st_sk.last_updated = datetime.utcnow()
                                            await db.flush()
                                        except Exception as _e_sk:
                                            logger.debug(f"[stats] skip write fail: {_e_sk}")
                                        done_actions += 1
                                        continue

                                    try:
                                        from models.channel_ban_stats import ChannelBanStats
                                        st = (await db.execute(
                                            select(ChannelBanStats).where(
                                                ChannelBanStats.user_id == campaign.user_id,
                                                ChannelBanStats.channel_username == target_channel,
                                            )
                                        )).scalar_one_or_none()
                                        if not st:
                                            st = ChannelBanStats(
                                                user_id=campaign.user_id,
                                                channel_username=target_channel,
                                                total_attempts=0, banned_count=0,
                                            )
                                            db.add(st)
                                        st.total_attempts += 1
                                        st.last_updated = datetime.utcnow()
                                    except Exception as e:
                                        logger.warning(f"[stats] Ошибка записи: {e}")

                                    logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] 💬 @{target_channel}: {comment_text[:60]}")
                                    # Отметка для правила о персональных отказах:
                                    # аккаунт, который только что написал, писать может.
                                    try:
                                        from utils.account_health import mark_wrote as _mw
                                        _mw(acc.id)
                                    except Exception:
                                        pass

                                    # ── ФЛАГ: успешный smart_comment доказывает что акк
                                    # в discussion group. Ставим/подкрепляем disc_joined
                                    # флаг чтобы будущие smart_comment не resкались зря.
                                    try:
                                        from utils.redis_pool import get_redis as _get_r_sc
                                        _get_r_sc().setex(
                                            f"gramgpt:disc_joined:{acc.id}:{target_channel.lstrip('@')}",
                                            30 * 86400, "1",
                                        )
                                    except Exception:
                                        pass
                                    # Глобальный реестр подписок
                                    try:
                                        from utils.subscriptions import record_discussion_join
                                        await record_discussion_join(db, acc.id, target_channel, via="comment")
                                    except Exception:
                                        pass

                                    if campaign:
                                        try:
                                            # АТОМАРНЫЙ инкремент через SQL — иначе при
                                            # параллельных сессиях (несколько акков комментят
                                            # одновременно) обычный campaign.comments_sent += 1
                                            # даёт race: session A и B читают 47, оба пишут 48,
                                            # теряется 1 инкремент. Инцидент 2026-07-14:
                                            # 81 CommentLog vs 47 comments_sent = потеряно 34.
                                            from sqlalchemy import update as _sa_update
                                            await db.execute(
                                                _sa_update(Campaign)
                                                .where(Campaign.id == campaign.id)
                                                .values(comments_sent=Campaign.comments_sent + 1)
                                            )
                                            # Сохраняем discussion IDs для verify-задачи
                                            _disc_peer_id = None
                                            _disc_msg_id = None
                                            try:
                                                _disc_msg_id = int(_sent_msg.id) if _sent_msg else None
                                                _pid = getattr(_sent_msg, "peer_id", None) if _sent_msg else None
                                                if _pid is not None:
                                                    _disc_peer_id = int(getattr(_pid, "channel_id", None) or
                                                                        getattr(_pid, "chat_id", None) or
                                                                        getattr(_pid, "user_id", None) or 0) or None
                                            except Exception:
                                                pass
                                            db.add(CommentLog(
                                                campaign_id=campaign.id, account_id=acc.id,
                                                account_phone=phone, channel_username=target_channel,
                                                channel_title="", post_id=target_post.id,
                                                post_text=post_text[:500],
                                                comment_text=comment_text,
                                                llm_provider=_val(campaign.llm_provider),
                                                discussion_peer_id=_disc_peer_id,
                                                discussion_msg_id=_disc_msg_id,
                                            ))
                                            await db.flush()
                                            await db.commit()
                                            logger.info(f"[plan][{phone}] ✅ CommentLog @{target_channel} записан в БД (камп {campaign.id})")

                                            # ── Насколько поздно мы пришли ──────────────
                                            # Первые комменты видит каждый, кто открыл
                                            # обсуждение, последние тонут внизу. Значит
                                            # ценность коммента зависит от того, сколько
                                            # их там уже было и как давно вышел пост.
                                            # Оба числа у нас уже на руках: дата поста и
                                            # счётчик ответов из GetDiscussionMessage —
                                            # лишних запросов не делаем.
                                            try:
                                                from datetime import timezone as _tz
                                                _pdate = getattr(target_post, "date", None)
                                                _age_min = None
                                                if _pdate:
                                                    _age_min = int(
                                                        (datetime.now(_tz.utc) - _pdate)
                                                        .total_seconds() // 60)
                                                # disc мог не создаться выше — тогда
                                                # NameError поймает общий except ниже.
                                                _before = None
                                                if disc and disc.messages:
                                                    _r = getattr(disc.messages[0], "replies", None)
                                                    if _r is not None:
                                                        _before = getattr(_r, "replies", None)
                                                await _safe_log(
                                                    db, task_id=log_task_id, account_id=acc.id,
                                                    action="comment_position",
                                                    detail="возраст поста %s мин, комментов до нас %s"
                                                           % (_age_min, _before),
                                                    channel=target_channel, success=True,
                                                    source=plan_source,
                                                    campaign_id=plan_campaign_id)
                                            except Exception:
                                                pass
                                        except Exception as log_err:
                                            logger.error(f"[plan][{phone}] ❌ Ошибка записи CommentLog: {log_err}")
                                            try: await db.rollback()
                                            except: pass

                                    await _safe_log(db, task_id=log_task_id, account_id=acc.id, action="smart_comment",
                                                     detail=f"💬 @{target_channel}: {comment_text[:80]}",
                                                     channel=target_channel, success=True,
                                                     source=plan_source, campaign_id=plan_campaign_id)

                                    done_actions += 1

                                except Exception as e:
                                    err = str(e)
                                    err_type = type(e).__name__
                                    logger.warning(f"[plan][{phone}]   [{action_num}/{len(actions)}] ❌ comment error ({err_type}): {err[:100]}")

                                    # ── Детектим бан двумя способами ─────
                                    # 1. По имени класса исключения (самый надёжный)
                                    ban_exception_classes = {
                                        "ChatWriteForbiddenError",
                                        "UserBannedInChannelError",
                                        "ChatRestrictedError",
                                        "ChannelPrivateError",
                                        "UserDeactivatedError",
                                        "UserDeactivatedBanError",
                                        "PeerIdInvalidError",  # часто означает "забанены/кикнуты"
                                    }
                                    # 2. По тексту сообщения (для случаев когда Telethon не задрафтил спецкласс)
                                    ban_message_patterns = [
                                        "can't write", "cannot write", "write in this chat",
                                        "banned from sending", "banned in this", "you're banned",
                                        "chat_write_forbidden", "user_banned", "user_restricted",
                                        "banned_rights", "channel_private", "you_blocked",
                                    ]
                                    is_ban = (
                                        err_type in ban_exception_classes or
                                        any(p.lower() in err.lower() for p in ban_message_patterns)
                                    )

                                    # ── Запись в channel_ban_stats ──
                                    # ВСЕГДА инкрементим total_attempts (даже на не-бан ошибках),
                                    # чтобы канал был виден в UI проходимости.
                                    # banned_count инкрементим только если is_ban.
                                    # transient-ошибки типа FLOOD_WAIT не считаем — это не вина канала.
                                    is_transient = (
                                        any(p in err for p in (
                                            "FLOOD_WAIT", "AUTH_KEY_UNREGISTERED",
                                            "PEER_FLOOD", "ServerError",
                                            "TimeoutError", "ConnectionError",
                                            # Telethon не знает новый Constructor ID от Telegram
                                            # (новая фича Telegram, нужен upgrade библиотеки).
                                            # Не вина канала, не вина аккаунта.
                                            "Could not find a matching Constructor",
                                            "TypeNotFoundError",
                                            # Подобные MTProto/parse ошибки которые сами по себе
                                            # не означают что нас банят
                                            "MTProtoError", "InvalidBufferError",
                                            "SecurityError",  # обычно битый пакет от сервера/прокси
                                        )) or
                                        err_type in (
                                            "TypeNotFoundError",
                                            "InvalidBufferError",
                                            "SecurityCheckMismatch",
                                            "AuthKeyDuplicatedError",  # отдельный кейс, аккаунт лезет с 2 мест
                                        )
                                    )

                                    if campaign and not is_transient:
                                        try:
                                            from models.channel_ban_stats import ChannelBanStats
                                            st = (await db.execute(
                                                select(ChannelBanStats).where(
                                                    ChannelBanStats.user_id == campaign.user_id,
                                                    ChannelBanStats.channel_username == target_channel,
                                                )
                                            )).scalar_one_or_none()
                                            if not st:
                                                st = ChannelBanStats(
                                                    user_id=campaign.user_id,
                                                    channel_username=target_channel,
                                                    total_attempts=0, banned_count=0,
                                                )
                                                db.add(st)
                                            st.total_attempts += 1
                                            if is_ban:
                                                st.banned_count += 1
                                                st.last_ban_reason = f"[{err_type}] {err[:180]}"
                                                logger.warning(f"[stats] 🚫 @{target_channel} БАН ({err_type}): {st.banned_count}/{st.total_attempts}")
                                            else:
                                                logger.info(f"[stats] ⚠ @{target_channel} ошибка-не-бан ({err_type}): {st.banned_count}/{st.total_attempts}")
                                            st.last_updated = datetime.utcnow()
                                        except Exception as se:
                                            logger.warning(f"[stats] Ошибка записи: {se}")

                                    await _safe_log(db, task_id=log_task_id, account_id=acc.id, action="smart_comment",
                                                     exc=e,
                                                     detail=f"❌ Ошибка @{target_channel}: {err[:100]}",
                                                     channel=target_channel, success=False, error=err[:200],
                                                     source=plan_source, campaign_id=plan_campaign_id,
                                                     _skip_health=True)

                                    # ── Здоровье аккаунта и кэш канала (2026-08-22) ──
                                    # record_error сам решает, чья это вина:
                                    #   severity='account'  → Telegram отчитал аккаунт
                                    #   severity='channel'  → виноват канал
                                    #   severity='transient'→ сеть/прокси, никто
                                    # Наши внутренние причины сюда не доходят вовсе.
                                    _sev = "none"
                                    try:
                                        from utils.account_health import record_error as _health_rec
                                        from utils.error_origin import classify_severity as _sev_of
                                        _sev = _sev_of(err, e)
                                        _hres = _health_rec(acc.id, err, e)
                                        if _sev == "channel":
                                            from utils.channel_caps import mark_bad as _cap_bad2
                                            await _cap_bad2(db, target_channel,
                                                            reason=type(e).__name__[:64],
                                                            account_id=acc.id, error=err)
                                    except Exception as _he:
                                        logger.debug(f"[health] rec fail: {_he}")
                                        _hres = {"action": "none"}

                                    # P1: TG framework freeze
                                    if await _check_tg_freeze(acc, db, err, phone):
                                        break

                                    _act = _hres.get("action", "none")
                                    if _act == "dead":
                                        logger.error(f"[plan][{phone}] сессия отозвана Telegram — frozen")
                                        acc.status = "frozen"
                                        break
                                    if _act == "frozen":
                                        acc.status = "frozen"
                                        break
                                    if _act == "rest":
                                        # PEER_FLOOD и крупный FLOOD_WAIT больше НЕ
                                        # списывают аккаунт навсегда: состояние обратимое,
                                        # ставим паузу (см. utils/account_health).
                                        logger.warning(
                                            f"[plan][{phone}] аккаунт уходит на отдых "
                                            f"{_hres.get('rest_sec', 0) // 3600}ч — сессия прервана"
                                        )
                                        break
                                    if _act == "wait":
                                        _w = _hres.get("rest_sec", 60)
                                        logger.warning(f"[plan][{phone}] FLOOD_WAIT {_w}с — пауза")
                                        await asyncio.sleep(_w + random.randint(5, 15))
                                        break
                                    if _sev == "channel":
                                        # Канал плохой, аккаунт здоров — идём к следующему
                                        # действию, сессию не рвём.
                                        pass

                            elif action_type == "react_to_comment":
                                target_channel = action.get("channel", "")
                                react_count = action.get("count", 1)

                                # ── Учёт отказов ────────────────────────────
                                # Действие отваливалось молча в семи местах, и
                                # в журнал попадали только успехи: 415 записей
                                # за неделю, все удачные. При этом в плане на
                                # день стояло 273 реакции, а доезжало 65 —
                                # куда делись остальные, узнать было негде.
                                # Теперь каждая причина пишется отдельно.
                                async def _react_skip(reason):
                                    try:
                                        await _safe_log(
                                            db, task_id=log_task_id,
                                            account_id=acc.id,
                                            action="react_to_comment",
                                            detail="пропуск: %s" % reason,
                                            channel=target_channel,
                                            success=False, error=reason,
                                            source=plan_source,
                                            campaign_id=plan_campaign_id)
                                    except Exception:
                                        pass

                                if not target_channel:
                                    continue
                                try:
                                    from telethon.tl.functions.messages import SendReactionRequest
                                    from telethon.tl.functions.channels import GetFullChannelRequest
                                    from telethon.tl.types import ReactionEmoji
                                    from telethon.errors import ReactionInvalidError

                                    entity = await client.get_entity(target_channel)

                                    # Берём последний пост канала
                                    posts = await client.get_messages(entity, limit=1)
                                    if not posts:
                                        await _react_skip("нет постов в канале")
                                        continue
                                    post = posts[0]

                                    # Ищем linked discussion group + available_reactions
                                    full = await client(GetFullChannelRequest(entity))
                                    linked_chat_id = getattr(full.full_chat, 'linked_chat_id', None)
                                    discussion = await client.get_entity(linked_chat_id) if linked_chat_id else entity

                                    # У discussion group свои available_reactions
                                    disc_full = full
                                    if linked_chat_id:
                                        try:
                                            disc_full = await client(GetFullChannelRequest(discussion))
                                        except Exception:
                                            pass

                                    allowed_emojis = []
                                    try:
                                        available = getattr(disc_full.full_chat, 'available_reactions', None)
                                        cls = type(available).__name__ if available is not None else "None"
                                        if cls == "ChatReactionsSome":
                                            for r in getattr(available, 'reactions', []):
                                                if hasattr(r, 'emoticon'):
                                                    allowed_emojis.append(r.emoticon)
                                        elif cls == "ChatReactionsNone":
                                            await _react_skip("реакции в чате запрещены")
                                            continue
                                        else:
                                            allowed_emojis = ["👍", "❤️", "🔥", "👏", "🤔", "😮", "🥰", "💯", "🎉"]
                                    except Exception:
                                        allowed_emojis = ["👍", "❤️", "🔥", "👏"]

                                    if not allowed_emojis:
                                        await _react_skip("нет разрешённых эмодзи")
                                        continue

                                    # Получаем комменты под постом.
                                    # ── ФИКС 2026-08-22 ───────────────────────────
                                    # Было reply_to=post.id (id поста В КАНАЛЕ). Для
                                    # peer=discussion нужен id топ-сообщения В ГРУППЕ —
                                    # другое число. Итог: либо MsgIdInvalidError, либо
                                    # реакции летели в ЧУЖОЙ тред. Тот же баг, что был
                                    # в post-send verify (см. коммент выше по файлу).
                                    _disc_top = None
                                    if linked_chat_id:
                                        try:
                                            from telethon.tl.functions.messages import (
                                                GetDiscussionMessageRequest as _GDMR_r,
                                            )
                                            _dm_r = await client(_GDMR_r(peer=entity, msg_id=post.id))
                                            if _dm_r and _dm_r.messages:
                                                _disc_top = _dm_r.messages[0].id
                                        except Exception:
                                            _disc_top = None
                                    else:
                                        # discussion == сам канал (обычная чат-группа)
                                        _disc_top = post.id
                                    if _disc_top is None:
                                        await _react_skip("не нашёл корень треда")
                                        continue

                                    # ── Берём ПЕРВЫЕ комменты треда ──────────────
                                    # reverse=True разворачивает обход: от старых к
                                    # новым. Первые комменты видит каждый, кто открыл
                                    # обсуждение, а последние тонут внизу. Раньше
                                    # бралось десять последних, и реакция уходила
                                    # туда, где её никто не увидит.
                                    _TOP_N = 4
                                    comments = []
                                    try:
                                        _me_id = getattr(await client.get_me(), "id", None)
                                    except Exception:
                                        _me_id = None
                                    try:
                                        async for cmsg in client.iter_messages(
                                                discussion, reply_to=_disc_top,
                                                limit=_TOP_N * 3, reverse=True):
                                            if not cmsg.text:
                                                continue
                                            if _me_id and getattr(cmsg, "sender_id", None) == _me_id:
                                                continue        # свой коммент не трогаем
                                            comments.append(cmsg)
                                            if len(comments) >= _TOP_N:
                                                break
                                    except Exception as _ie:
                                        # Тред могли снести — MsgIdInvalidError прилетает
                                        # из самого обхода и без этого уносит канал целиком.
                                        logger.debug(f"[react] обход треда @{target_channel}: {type(_ie).__name__}")
                                        await _react_skip("тред не читается: %s" % type(_ie).__name__)
                                        continue

                                    if not comments:
                                        await _react_skip("нет чужих комментов в треде")
                                        continue

                                    targets = random.sample(comments, min(react_count, len(comments)))

                                    for ci, cmsg in enumerate(targets):
                                        # До 3 разных эмодзи на случай если первый отвергнут
                                        candidates = random.sample(allowed_emojis, min(3, len(allowed_emojis)))
                                        reacted_emoji = None
                                        for emoji in candidates:
                                            try:
                                                await client(SendReactionRequest(
                                                    peer=discussion,
                                                    msg_id=cmsg.id,
                                                    reaction=[ReactionEmoji(emoticon=emoji)],
                                                ))
                                                reacted_emoji = emoji
                                                break
                                            except ReactionInvalidError:
                                                continue

                                        if reacted_emoji:
                                            logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] 😍 Реакция {reacted_emoji} на коммент в @{target_channel}")
                                            # ── ФЛАГ: успешная реакция в discussion group
                                            # ДОКАЗЫВАЕТ что акк туда попал. Ставим disc_joined
                                            # флаг чтобы hard_guard smart_comment не резал зря
                                            # (инцидент 2026-07-16: 123 skip/день зря).
                                            try:
                                                from utils.redis_pool import get_redis as _get_r_rc
                                                _get_r_rc().setex(
                                                    f"gramgpt:disc_joined:{acc.id}:{target_channel.lstrip('@')}",
                                                    30 * 86400, "1",
                                                )
                                            except Exception:
                                                pass
                                            # Глобальный реестр подписок
                                            try:
                                                from utils.subscriptions import record_discussion_join
                                                await record_discussion_join(db, acc.id, target_channel, via="react")
                                            except Exception:
                                                pass
                                            await _safe_log(db, task_id=log_task_id, account_id=acc.id, action="react_to_comment",
                                                             detail=f"Реакция {reacted_emoji} на коммент в @{target_channel}",
                                                             channel=target_channel, emoji=reacted_emoji, success=True,
                                                             source=plan_source, campaign_id=plan_campaign_id)
                                            done_actions += 1
                                        if ci < len(targets) - 1:
                                            await asyncio.sleep(random.randint(3, 10))

                                except Exception as e:
                                    logger.warning(f"[plan][{phone}]   [{action_num}/{len(actions)}] react_to_comment ошибка: {str(e)[:100]}")
                                    await _react_skip("сбой %s" % type(e).__name__)

                            elif action_type == "join_discussion_of":
                                # Отдельное действие: вступить в linked discussion group
                                # канала. Спрятано в сессии между обычными действиями
                                # (read_feed, view_stories и т.д.) чтобы выглядеть
                                # органично. Не сразу после join_target_channel — а
                                # в СЛЕДУЮЩИХ сессиях через plan_generator sprinkle.
                                _dch = action.get("channel", "")
                                if not _dch:
                                    continue
                                _dclean = _dch.lstrip("@").strip()

                                # ── Нужно ли вообще вступать? ──────────────
                                # У большинства каналов писать в обсуждение
                                # можно и без членства: коммент уходит через
                                # контекст поста. Флаг join_to_send говорит,
                                # требует ли группа вступления, и он уже лежит
                                # в кэше свойств — читаем базу, в Telegram ни
                                # одного запроса.
                                # Замер 25.08 по кампании #139: из 270 вступлений
                                # 220 (83%) были не нужны, и именно они дали
                                # почти весь шум disc_join_refused/disc_unresolved.
                                # join_to_send=None (не выяснено) — пробуем, как раньше.
                                try:
                                    from utils.channel_caps import get_cap as _gc_d
                                    _cap_d = await _gc_d(db, _dclean)
                                    if _cap_d is not None and _cap_d.join_to_send is False:
                                        logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] 💬 @{_dclean}: вступление не требуется — пропуск")
                                        done_actions += 1
                                        continue
                                except Exception:
                                    pass

                                try:
                                    from telethon.tl.functions.channels import (
                                        GetFullChannelRequest as _GFCd,
                                        JoinChannelRequest as _JCd,
                                    )
                                    from telethon.errors import (
                                        UserAlreadyParticipantError as _UAPd,
                                        FloodWaitError as _FWd,
                                        InviteRequestSentError as _IRSEd,
                                        ChannelPrivateError as _CPEd,
                                    )
                                    from utils.redis_pool import get_redis as _get_r_d
                                    _r_d = _get_r_d()
                                    # 2026-07-23 — проверка invite_pending флага.
                                    # Если ранее InviteRequestSent → discussion закрыт,
                                    # заявка на рассмотрении админа. Не пытаемся снова
                                    # (иначе тратим API-запросы впустую).
                                    # Уже знаем, что прямой join в эту группу закрыт —
                                    # не тратим запрос (см. _CPEd ниже).
                                    try:
                                        if _r_d.get(f"gramgpt:disc_join_refused:{_dclean}"):
                                            logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] 💬 @{_dclean}: join закрыт (знаем) — пропуск")
                                            done_actions += 1
                                            continue
                                    except Exception:
                                        pass
                                    _invite_key = f"gramgpt:disc_invite_pending:{acc.id}:{_dclean}"
                                    try:
                                        if _r_d.get(_invite_key):
                                            logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] 💬 @{_dclean}: invite pending — skip")
                                            await _safe_log(db, task_id=log_task_id, account_id=acc.id, action="join_discussion_of",
                                                             detail=f"💬 @{_dclean}: invite pending (was rejected earlier)",
                                                             channel=_dclean, success=False, error="invite_pending",
                                                             source=plan_source, campaign_id=plan_campaign_id)
                                            done_actions += 1
                                            continue
                                    except Exception:
                                        pass
                                    # Redis-кеш linked_chat_id — переиспользуется
                                    # с join_target_channel/smart_comment fallback.
                                    _link_id_d = None
                                    _ck_d = f"gramgpt:linked_chat:{_dclean}"
                                    try:
                                        _cv = _r_d.get(_ck_d)
                                        if _cv:
                                            _link_id_d = int(_cv.decode() if isinstance(_cv, bytes) else _cv)
                                    except Exception:
                                        pass
                                    if _link_id_d is None:
                                        _e_d = await client.get_entity(_dclean)
                                        _f_d = await client(_GFCd(_e_d))
                                        _link_id_d = getattr(_f_d.full_chat, "linked_chat_id", 0) or 0
                                        try:
                                            _r_d.setex(_ck_d, 24 * 3600, str(_link_id_d))
                                        except Exception:
                                            pass
                                    if not _link_id_d:
                                        logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] 💬 @{_dclean}: нет discussion — skip")
                                        await _safe_log(db, task_id=log_task_id, account_id=acc.id, action="join_discussion_of",
                                                         detail=f"💬 @{_dclean}: нет discussion group",
                                                         channel=_dclean, success=False, error="no_linked_chat",
                                                         source=plan_source, campaign_id=plan_campaign_id)
                                        done_actions += 1
                                        continue
                                    # Резолвим discussion ПРАВИЛЬНО: access_hash
                                    # берём из full.chats, а не пытаемся достать
                                    # группу по голому id (для не-участника это
                                    # всегда ChannelPrivateError — см. коммент в
                                    # utils/channel_caps.resolve_discussion_entity).
                                    from utils.channel_caps import resolve_discussion_entity as _rde
                                    try:
                                        _disc_ent_d = await _rde(client, _dclean, link_id=_link_id_d)
                                    except _CPEd:
                                        # Сам канал недоступен ЭТОМУ аккаунту (обычно он
                                        # там забанен). Не наша поломка и не свойство
                                        # канала — пишем понятной причиной, а не сырым
                                        # «канал приватный».
                                        logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] 💬 @{_dclean}: канал недоступен этому акку — skip")
                                        await _safe_log(db, task_id=log_task_id, account_id=acc.id, action="join_discussion_of",
                                                         detail=f"💬 @{_dclean}: канал недоступен этому аккаунту",
                                                         channel=_dclean, success=False,
                                                         error="acc_no_access", error_origin="internal",
                                                         source=plan_source, campaign_id=plan_campaign_id)
                                        done_actions += 1
                                        continue
                                    if _disc_ent_d is None:
                                        logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] 💬 @{_dclean}: discussion не резолвится — skip")
                                        await _safe_log(db, task_id=log_task_id, account_id=acc.id, action="join_discussion_of",
                                                         detail=f"💬 @{_dclean}: discussion не резолвится",
                                                         channel=_dclean, success=False, error="disc_unresolved",
                                                         error_origin="internal",
                                                         source=plan_source, campaign_id=plan_campaign_id)
                                        done_actions += 1
                                        continue
                                    _already_in_disc = False
                                    try:
                                        await client(_JCd(_disc_ent_d))
                                        logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] 💬 join discussion @{_dclean}")
                                    except _UAPd:
                                        _already_in_disc = True
                                        logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] 💬 уже в discussion @{_dclean}")
                                    except _CPEd:
                                        # ── Группа не пускает по прямому join ──
                                        # Это НЕ поломка и НЕ повод считать канал
                                        # непригодным. Замер 2026-08-25 @UaOnlii:
                                        # JoinChannelRequest отвечает
                                        # ChannelPrivateError, но ветка комментов
                                        # того же канала спокойно читается, и
                                        # коммент туда уходит — send_message с
                                        # comment_to не требует членства в группе.
                                        # То есть явное вступление здесь просто
                                        # лишнее. Раньше это писалось как
                                        # ChannelPrivateError и выглядело в логах
                                        # как «канал недоступен».
                                        # Запоминаем на неделю, чтобы не долбиться
                                        # в этот join повторно.
                                        try:
                                            _r_d.setex(f"gramgpt:disc_join_refused:{_dclean}",
                                                       7 * 86400, "1")
                                        except Exception:
                                            pass
                                        logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] 💬 @{_dclean}: прямой join закрыт — комментить всё равно можно")
                                        await _safe_log(db, task_id=log_task_id, account_id=acc.id, action="join_discussion_of",
                                                         detail=f"💬 @{_dclean}: прямой join закрыт, комментирование доступно",
                                                         channel=_dclean, success=False,
                                                         error="disc_join_refused", error_origin="internal",
                                                         source=plan_source, campaign_id=plan_campaign_id)
                                        done_actions += 1
                                        await asyncio.sleep(random.uniform(2, 5))
                                        continue
                                    # ── ФЛАГ: теперь smart_comment на этот
                                    # канал разрешён (для этого акка). TTL 30 дней.
                                    try:
                                        _r_d.setex(
                                            f"gramgpt:disc_joined:{acc.id}:{_dclean}",
                                            30 * 86400, "1",
                                        )
                                    except Exception:
                                        pass
                                    # Глобальный реестр подписок
                                    try:
                                        from utils.subscriptions import record_discussion_join
                                        await record_discussion_join(db, acc.id, _dclean, via="join_discussion")
                                    except Exception:
                                        pass
                                    await _safe_log(db, task_id=log_task_id, account_id=acc.id, action="join_discussion_of",
                                                     detail=f"💬 {'уже в' if _already_in_disc else 'вступил в'} discussion @{_dclean}",
                                                     channel=_dclean, success=True,
                                                     source=plan_source, campaign_id=plan_campaign_id)
                                    done_actions += 1
                                    _join_fail_count = 0   # P3: reset streak
                                    await asyncio.sleep(random.uniform(3, 8))
                                except _FWd as _fwd:
                                    logger.warning(f"[plan][{phone}]   [{action_num}/{len(actions)}] 💬 FloodWait {_fwd.seconds}с @{_dclean} — стоп сессии")
                                    await _safe_log(db, task_id=log_task_id, account_id=acc.id, action="join_discussion_of",
                                                     detail=f"💬 FloodWait {_fwd.seconds}с @{_dclean}",
                                                     channel=_dclean, success=False, error=f"FloodWait {_fwd.seconds}",
                                                     source=plan_source, campaign_id=plan_campaign_id)
                                    break    # выходим из всей сессии — не рискуем
                                except _IRSEd:
                                    # Discussion group закрыт для свободного вступления,
                                    # заявка ушла админу. Помечаем в Redis чтобы не пытаться
                                    # снова 30 дней (иначе rebuild будет ставить в план опять).
                                    logger.warning(f"[plan][{phone}]   [{action_num}/{len(actions)}] 💬 InviteRequestSent @{_dclean} — помечаем на 30 дней + канал в blacklist")
                                    try:
                                        _r_d.setex(_invite_key, 30 * 86400, "1")
                                    except Exception:
                                        pass
                                    await _safe_log(db, task_id=log_task_id, account_id=acc.id, action="join_discussion_of",
                                                     detail=f"💬 InviteRequestSent @{_dclean} — closed discussion",
                                                     channel=_dclean, success=False, error="InviteRequestSentError",
                                                     source=plan_source, campaign_id=plan_campaign_id)
                                    # Discussion закрыт — комментить нельзя. Blacklist канала.
                                    await _blacklist_bad_channel(db, _dclean, reason="invite_pending_disc")
                                    done_actions += 1
                                    continue
                                except Exception as _de2:
                                    # ── БЫЛО logger.debug (невидимо) → теперь warning + WarmupLog
                                    # Инцидент 2026-07-16: тихие exception'ы здесь не давали
                                    # поставить disc_joined флаг → 123 skip smart_comment/день.
                                    _de2_err = str(_de2)
                                    logger.warning(f"[plan][{phone}]   [{action_num}/{len(actions)}] 💬 join_discussion_of @{_dclean} FAIL: {type(_de2).__name__}: {_de2_err[:120]}")
                                    await _safe_log(db, task_id=log_task_id, account_id=acc.id, action="join_discussion_of",
                                                     detail=f"❌ join_discussion_of @{_dclean}: {type(_de2).__name__}",
                                                     channel=_dclean, success=False, error=_de2_err[:200],
                                                     source=plan_source, campaign_id=plan_campaign_id)
                                    # P1: TG framework freeze
                                    if await _check_tg_freeze(acc, db, _de2_err, phone):
                                        break
                                    # P2: канал не существует ("No user has X as username")
                                    if "No user has" in _de2_err or "UsernameNotOccupied" in _de2_err or "UsernameInvalid" in _de2_err:
                                        await _blacklist_bad_channel(db, _dclean, reason="disc_resolve_missing")
                                    # P3: подряд-фейлы
                                    _join_fail_count += 1
                                    if _join_fail_count >= 3:
                                        _set_join_pause(acc.id, phone, "3 consecutive join fails (disc)")
                                        await _safe_log(db, task_id=log_task_id, account_id=acc.id, action="error",
                                                         detail="3 подряд join-fails → pause 30мин",
                                                         success=False, error="join_fail_streak",
                                                         source=plan_source, campaign_id=plan_campaign_id)
                                        break
                                continue

                            elif action_type == "join_target_channel":
                                # Вступление в конкретный канал кампании
                                ch = action.get("channel", "")
                                if not ch:
                                    continue
                                try:
                                    from telethon.tl.functions.channels import JoinChannelRequest, GetParticipantRequest
                                    from telethon.tl.functions.contacts import ResolveUsernameRequest
                                    from telethon.errors import (
                                        UserAlreadyParticipantError, InviteRequestSentError,
                                        ChannelsTooMuchError, ChannelPrivateError,
                                        UsernameNotOccupiedError, UsernameInvalidError,
                                    )
                                    from models.campaign_channel_assignment import CampaignChannelAssignment

                                    clean_ch = ch.lstrip('@').strip()
                                    me = await client.get_me()

                                    # 1) Резолвим канал свежим запросом (минуем кэш Telethon)
                                    try:
                                        resolved = await client(ResolveUsernameRequest(clean_ch))
                                        if not resolved.chats:
                                            raise Exception(f"@{clean_ch}: канал не существует")
                                        entity = resolved.chats[0]
                                    except (UsernameNotOccupiedError, UsernameInvalidError) as e:
                                        # P2: канала нет в TG → чёрный список чтобы
                                        # больше никогда не попал в план ни одному акку
                                        await _blacklist_bad_channel(db, clean_ch, reason=f"resolve_{type(e).__name__}")
                                        raise Exception(f"@{clean_ch}: невалидный username")

                                    # 2) Уже подписаны?
                                    already_in = False
                                    try:
                                        await client(GetParticipantRequest(channel=entity, participant=me))
                                        already_in = True
                                        logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] 📢 @{clean_ch}: уже подписан (проверено)")
                                    except Exception:
                                        already_in = False

                                    # 3) Не подписаны → JoinChannelRequest + верификация
                                    if not already_in:
                                        join_request_sent = False
                                        try:
                                            await client(JoinChannelRequest(entity))
                                            join_request_sent = True
                                        except UserAlreadyParticipantError:
                                            already_in = True
                                        except InviteRequestSentError:
                                            raise Exception(f"@{clean_ch}: канал требует подтверждения (отправлена заявка)")
                                        except ChannelsTooMuchError:
                                            raise Exception(f"@{clean_ch}: лимит каналов на аккаунте")
                                        except ChannelPrivateError:
                                            raise Exception(f"@{clean_ch}: приватный канал")

                                        # 4) Проверяем что мы реально стали участником (один раз)
                                        if join_request_sent and not already_in:
                                            await asyncio.sleep(random.uniform(2, 4))
                                            try:
                                                await client(GetParticipantRequest(channel=entity, participant=me))
                                                logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] 📢 Вступил @{clean_ch} (подтверждено)")
                                            except Exception as verr:
                                                # Не в участниках после join → теневой бан / заморозка / флудвейт
                                                raise Exception(f"@{clean_ch}: join не прошёл (теневой бан/заморозка): {type(verr).__name__}")

                                    # 5) Обновляем dialogs cache чтобы next read_feed не словил MsgidDecreaseRetry
                                    try:
                                        await client.get_dialogs(limit=1, archived=False)
                                    except Exception:
                                        pass

                                    # 6) Сохраняем assignment как joined
                                    if plan.campaign_id:
                                        assignment = (await db.execute(
                                            select(CampaignChannelAssignment).where(
                                                CampaignChannelAssignment.campaign_id == plan.campaign_id,
                                                CampaignChannelAssignment.account_id  == acc.id,
                                                CampaignChannelAssignment.channel_username == ch,
                                            )
                                        )).scalar_one_or_none()
                                        if assignment:
                                            assignment.status    = "joined"
                                            assignment.joined_at = datetime.utcnow()
                                        else:
                                            db.add(CampaignChannelAssignment(
                                                campaign_id=plan.campaign_id,
                                                account_id=acc.id,
                                                channel_username=ch,
                                                status="joined",
                                                joined_at=datetime.utcnow(),
                                            ))
                                        await db.flush()

                                    # Глобальный реестр подписок для anti-ban
                                    try:
                                        from utils.subscriptions import record_channel_join
                                        _ch_id = getattr(entity, 'id', None)
                                        await record_channel_join(db, acc.id, clean_ch, via="join_target", channel_id=_ch_id)
                                    except Exception:
                                        pass

                                    detail = f"Уже был в @{clean_ch}" if already_in else f"Вступил в @{clean_ch} (подтверждено)"
                                    await _safe_log(db, task_id=log_task_id, account_id=acc.id, action="join_channel",
                                                     detail=detail, channel=clean_ch, success=True,
                                                     source=plan_source, campaign_id=plan_campaign_id)
                                    done_actions += 1
                                    _join_fail_count = 0   # P3: reset streak
                                except Exception as e:
                                    err = str(e)
                                    logger.warning(f"[plan][{phone}]   [{action_num}/{len(actions)}] 📢 join_target_channel ошибка ({type(e).__name__}) @{ch}: {err[:120]}")
                                    # Фиксируем ошибку в матрице
                                    if plan.campaign_id:
                                        try:
                                            from models.campaign_channel_assignment import CampaignChannelAssignment
                                            assignment = (await db.execute(
                                                select(CampaignChannelAssignment).where(
                                                    CampaignChannelAssignment.campaign_id == plan.campaign_id,
                                                    CampaignChannelAssignment.account_id  == acc.id,
                                                    CampaignChannelAssignment.channel_username == ch,
                                                )
                                            )).scalar_one_or_none()
                                            if assignment:
                                                assignment.status = "failed"
                                                await db.flush()
                                        except Exception:
                                            pass
                                    # Логируем ошибку в warmup_logs чтобы было видно в UI
                                    await _safe_log(db, task_id=log_task_id, account_id=acc.id, action="join_channel",
                                                     detail=f"❌ @{ch}: {err[:120]}",
                                                     channel=ch, success=False, error=err[:200],
                                                     source=plan_source, campaign_id=plan_campaign_id)
                                    # P1: TG framework freeze → сразу frozen + break session
                                    if await _check_tg_freeze(acc, db, err, phone):
                                        break
                                    # P3: consecutive join fails
                                    _join_fail_count += 1
                                    if _join_fail_count >= 3:
                                        _set_join_pause(acc.id, phone, "3 consecutive join fails")
                                        await _safe_log(db, task_id=log_task_id, account_id=acc.id, action="error",
                                                         detail="3 подряд join-fails → pause 30мин",
                                                         success=False, error="join_fail_streak",
                                                         source=plan_source, campaign_id=plan_campaign_id)
                                        break
                                    import re as _re
                                    if "FLOOD_WAIT" in err:
                                        wait = int(_re.search(r"(\d+)", err).group(1)) if _re.search(r"(\d+)", err) else 60
                                        await asyncio.sleep(wait + random.randint(5, 15))
                                        break

                            elif action_type == "warmup_subscribe":
                                # WARMUP drip-подписка на КОНКРЕТНЫЙ target-канал.
                                # Подписывается + верифицирует + пишет в
                                # WarmupTask.subscribed_channels (для импорта в кампанию).
                                # НЕ создаёт CampaignChannelAssignment.
                                ch = action.get("channel", "")
                                if not ch or not plan.warmup_task_id:
                                    continue
                                clean_ch = ch.lstrip('@').strip()
                                try:
                                    from telethon.tl.functions.contacts import ResolveUsernameRequest
                                    from telethon.tl.functions.channels import JoinChannelRequest as _JCR, GetParticipantRequest
                                    from telethon.errors import UserAlreadyParticipantError, FloodWaitError
                                    me = await client.get_me()
                                    resolved = await client(ResolveUsernameRequest(clean_ch))
                                    if not resolved.chats:
                                        raise Exception("канал не найден")
                                    entity = resolved.chats[0]

                                    already_in = False
                                    try:
                                        await client(GetParticipantRequest(channel=entity, participant=me))
                                        already_in = True
                                    except Exception:
                                        pass

                                    if not already_in:
                                        await client(_JCR(entity))
                                        await asyncio.sleep(random.uniform(2, 4))
                                        await client(GetParticipantRequest(channel=entity, participant=me))

                                    from models.warmup import WarmupTask as _WT
                                    wt = (await db.execute(
                                        select(_WT).where(_WT.id == plan.warmup_task_id)
                                    )).scalar_one_or_none()
                                    if wt:
                                        subs = dict(wt.subscribed_channels or {})
                                        subs[clean_ch] = datetime.utcnow().isoformat()
                                        wt.subscribed_channels = subs
                                        wt.channels_joined = (wt.channels_joined or 0) + 1
                                        await db.flush()

                                    logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] 📢 Drip @{clean_ch}" + (" (был)" if already_in else ""))
                                    await _safe_log(db, task_id=log_task_id, account_id=acc.id, action="drip_subscribe",
                                                     detail=f"Подписался на @{clean_ch}" + (" (уже был)" if already_in else " (новый)"),
                                                     channel=clean_ch, success=True,
                                                     source=plan_source, campaign_id=plan_campaign_id)
                                    done_actions += 1
                                except Exception as e:
                                    err = str(e)
                                    logger.warning(f"[plan][{phone}]   [{action_num}/{len(actions)}] 📢 drip @{clean_ch} err ({type(e).__name__}): {err[:100]}")
                                    await _safe_log(db, task_id=log_task_id, account_id=acc.id, action="drip_subscribe",
                                                     detail=f"❌ @{clean_ch}: {err[:100]}", channel=clean_ch,
                                                     success=False, error=err[:200],
                                                     source=plan_source, campaign_id=plan_campaign_id)
                                    import re as _re2
                                    if "FLOOD_WAIT" in err:
                                        wait = int(_re2.search(r"(\d+)", err).group(1)) if _re2.search(r"(\d+)", err) else 60
                                        await asyncio.sleep(wait + random.randint(5, 15))
                                        break

                            elif action_type == "join_channel":
                                # DECOY-подписка: случайный популярный канал, в который
                                # НЕ комментируем. Чистый шум. НЕ создаёт CampaignChannelAssignment,
                                # поэтому smart_comment guard сюда коммент не пустит.
                                try:
                                    from telethon.tl.functions.channels import JoinChannelRequest
                                    from telethon.errors import (
                                        UserAlreadyParticipantError, FloodWaitError,
                                        ChannelsTooMuchError, ChannelPrivateError,
                                    )
                                    # Широкий пул нейтральных каналов — чтобы разные аккаунты
                                    # не лезли в одни и те же 10 каналов (это тоже паттерн).
                                    DECOY_POOL = [
                                        "telegram", "durov", "techcrunch", "bbcnews", "reddit",
                                        "nytimes", "theverge", "mashable", "wired", "engadget",
                                        "sciencedaily", "nationalgeographic", "historyfacts",
                                        "TheEconomist", "Forbes", "business", "Bloomberg", "Reuters",
                                        "espn", "NBASphere", "footballtweet", "goal",
                                        "netflix", "PrimeVideo", "Spotify", "YouTube",
                                        "ProductHunt", "github", "hackernewsbot", "javascript_nuggets",
                                        "designer_news", "uxdesignweekly", "photography",
                                        "traveltips", "natgeotravel", "cookingdoc", "foodporn",
                                        "MarvelStudios", "DCComics", "GameSpot", "IGN", "steam",
                                        "cryptonews", "CoinDesk", "cointelegraph", "binance",
                                    ]
                                    ch = random.choice(DECOY_POOL)
                                    is_decoy = action.get("decoy", False)
                                    try:
                                        await client(JoinChannelRequest(ch))
                                    except UserAlreadyParticipantError:
                                        pass  # уже подписан — норм
                                    except (ChannelsTooMuchError, ChannelPrivateError):
                                        raise
                                    except FloodWaitError as fe:
                                        logger.warning(f"[plan][{phone}] decoy join FloodWait {fe.seconds}с")
                                        await asyncio.sleep(fe.seconds + random.randint(5, 15))
                                        raise
                                    label = "🎭 Decoy-подписка" if is_decoy else "📢 Подписался"
                                    logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] {label} на @{ch}")
                                    # Глобальный реестр подписок (в т.ч. decoy — потом
                                    # не будем в декоях повторно подписываться)
                                    try:
                                        from utils.subscriptions import record_channel_join
                                        await record_channel_join(db, acc.id, ch, via=("decoy" if is_decoy else "warmup"))
                                    except Exception:
                                        pass
                                    await _safe_log(db, task_id=log_task_id, account_id=acc.id, action="join_channel",
                                                     detail=f"{label} на @{ch} (не комментим)",
                                                     channel=ch, success=True,
                                                     source=plan_source, campaign_id=plan_campaign_id)
                                    done_actions += 1
                                except Exception:
                                    pass

                            elif action_type == "typing":
                                try:
                                    me = await client.get_me()
                                    from telethon.tl.functions.messages import SetTypingRequest
                                    from telethon.tl.types import SendMessageTypingAction
                                    await client(SetTypingRequest(peer=me, action=SendMessageTypingAction()))
                                    dur = action.get("duration", random.randint(2, 10))
                                    logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] ⌨️ Typing {dur}с")
                                    await asyncio.sleep(dur)
                                    done_actions += 1
                                except Exception:
                                    pass

                            pause = action.get("pause_after", random.randint(3, 30))
                            await asyncio.sleep(pause)

                            await db.flush()

                        except Exception as e:
                            err = str(e)
                            logger.warning(f"[plan][{phone}]   [{action_num}/{len(actions)}] ❌ {action.get('type')}: {err[:80]}")

                            # Дублируем как ошибку аккаунта в AccountErrorLog
                            try:
                                from utils.account_errors import log_account_error_async
                                await log_account_error_async(
                                    db=db, account_id=acc.id, user_id=acc.user_id,
                                    source=plan_source, exc=e,
                                    message=f"action={action.get('type')}: {err[:300]}",
                                    proxy_id=getattr(acc, "proxy_id", None),
                                    campaign_id=plan_campaign_id,
                                    warmup_task_id=plan.warmup_task_id,
                                )
                            except Exception:
                                pass

                            # P1: TG framework freeze (любой action)
                            if await _check_tg_freeze(acc, db, err, phone):
                                break

                            # ── Единая точка решения (2026-08-22) ──
                            # Раньше здесь PEER_FLOOD одинаково с
                            # AUTH_KEY_UNREGISTERED ставил frozen, то есть
                            # списывал живой аккаунт за обратимое состояние.
                            # Теперь решает account_health по severity.
                            try:
                                from utils.account_health import record_error as _health_rec2
                                _hres2 = _health_rec2(acc.id, err, e)
                            except Exception:
                                _hres2 = {"action": "none"}

                            _act2 = _hres2.get("action", "none")
                            if _act2 in ("dead", "frozen"):
                                logger.error(f"[plan][{phone}] {err[:40]} — аккаунт frozen")
                                acc.status = "frozen"
                                break
                            if _act2 == "rest":
                                logger.warning(
                                    f"[plan][{phone}] отдых {_hres2.get('rest_sec', 0) // 3600}ч "
                                    f"— сессия прервана"
                                )
                                break
                            if _act2 == "wait":
                                await asyncio.sleep(_hres2.get("rest_sec", 60) + random.randint(5, 15))
                                break

                except Exception as e:
                    logger.error(f"[plan][{phone}] Session error: {e}")
                    # ── Сетевой обрыв → возвращаем суточный слот ──────────
                    # Слот занимается ДО подключения и до сих пор освобождался
                    # только на ранних выходах. Если сессия падала на самом
                    # коннекте, слот сгорал впустую: аккаунт терял одно из
                    # шести суточных подключений, ничего не сделав.
                    # Возвращаем только при сетевой ошибке — претензии
                    # Telegram слот не возвращают, там аккаунту и правда
                    # лучше передохнуть.
                    if "connection to telegram failed" in str(e).lower():
                        _session_never_started = True
                        try:
                            from utils.connection_limiter import release_connection as _rc
                            _rc(acc.id)
                            logger.info(f"[plan][{phone}] сетевой обрыв — слот подключения возвращён")
                        except Exception:
                            pass
                    await _safe_log(db, task_id=log_task_id, account_id=acc.id, action="error",
                                     detail=f"Ошибка сессии: {str(e)[:200]}", success=False,
                                     source=plan_source, campaign_id=plan_campaign_id)
                    try:
                        from utils.account_errors import log_account_error_async
                        await log_account_error_async(
                            db=db, account_id=acc.id, user_id=acc.user_id,
                            source=plan_source, exc=e,
                            message=f"Ошибка сессии: {str(e)[:300]}",
                            proxy_id=getattr(acc, "proxy_id", None),
                            campaign_id=plan_campaign_id,
                            warmup_task_id=plan.warmup_task_id,
                        )
                    except Exception:
                        pass
                finally:
                    # Защищаем от NameError если client не успел создаться
                    # (например, exception до make_telethon_client) И от
                    # AttributeError если объект — пустой.
                    try:
                        if client is not None:
                            await client.disconnect()
                    except Exception:
                        pass
                    finally:
                        # Принудительно закрываем session-файл — без этого
                        # на Windows .session держится залоченным даже
                        # после disconnect (sqlite + WAL).
                        try:
                            if client is not None and getattr(client, "session", None):
                                client.session.close()
                        except Exception:
                            pass

                logger.info(f"[plan][{phone}] ═══ Сессия {sess_num}/{total_sess} завершена: {done_actions}/{len(actions)} действий ═══")
                await _safe_log(db,
                    task_id=log_task_id, account_id=acc.id,
                    action="session_end",
                    detail=f"Сессия завершена: {done_actions} действий (план день {plan.day_number})",
                    success=True, created_at=datetime.utcnow(),
                    source=plan_source, campaign_id=plan_campaign_id,
                )

                # Обновляем счётчики/состояние WarmupTask ТОЛЬКО если это прогрев
                if plan.warmup_task_id and plan_source == 'warmup':
                    wt = (await db.execute(
                        select(WarmupTask).where(WarmupTask.id == plan.warmup_task_id)
                    )).scalar_one_or_none()
                    if wt:
                        now_utc = datetime.utcnow()
                        # Новый день плана → сбрасываем today-счётчики
                        if (wt.day or 0) != plan.day_number:
                            wt.day = plan.day_number
                            wt.day_started_at = now_utc
                            wt.today_actions = 0
                            wt.joined_today = 0

                        wt.actions_done = (wt.actions_done or 0) + done_actions
                        wt.today_actions = (wt.today_actions or 0) + done_actions
                        # today_limit = сколько действий запланировано на сегодня (для прогресс-бара)
                        wt.today_limit = sum(len(s.get("actions", [])) for s in sessions) or wt.today_limit

                        # next_action_at = время следующей невыполненной сессии (для карточки).
                        # Времена сессий хранятся в локальном UTC+3, next_action_at — в UTC.
                        from datetime import datetime as _dt, time as _time
                        next_idx = plan.executed_idx + 1
                        if next_idx < len(sessions):
                            # Ещё есть сессии СЕГОДНЯ
                            ns = sessions[next_idx]
                            local_today = (now_utc + timedelta(hours=3)).date()
                            local_dt = _dt.combine(local_today, _time(
                                hour=min(23, ns.get("connect_at_hour", 10)),
                                minute=ns.get("connect_at_minute", 0),
                            ))
                            wt.next_action_at = local_dt - timedelta(hours=3)  # UTC+3 → UTC
                        else:
                            # Сессии сегодня кончились — берём ПЕРВУЮ сессию следующего дня плана
                            next_plan = (await db.execute(
                                select(CampaignPlan).where(
                                    CampaignPlan.warmup_task_id == plan.warmup_task_id,
                                    CampaignPlan.day_number == plan.day_number + 1,
                                )
                            )).scalar_one_or_none()
                            np_sessions = (next_plan.plan.get("sessions", []) if next_plan else [])
                            if next_plan and np_sessions:
                                ns0 = np_sessions[0]
                                local_dt = _dt.combine(next_plan.plan_date, _time(
                                    hour=min(23, ns0.get("connect_at_hour", 10)),
                                    minute=ns0.get("connect_at_minute", 0),
                                ))
                                wt.next_action_at = local_dt - timedelta(hours=3)  # UTC+3 → UTC
                            else:
                                # следующего дня нет (прогрев заканчивается) — ничего
                                wt.next_action_at = None

                        # Прогрев завершён? (последний день + все сессии)
                        if (plan.executed_idx + 1) >= len(sessions) and plan.day_number >= (wt.total_days or 7):
                            wt.status = "finished"
                            wt.finished_at = now_utc

                        logger.info(f"[plan][{phone}] ✅ WarmupTask #{wt.id}: день {wt.day}, +{done_actions} (сегодня {wt.today_actions}/{wt.today_limit}, всего {wt.actions_done})")
                elif plan.campaign_id and plan_source == 'commenting':
                    logger.info(f"[plan][{phone}] 📝 Commenting сессия — WarmupTask НЕ трогаем (actions: {done_actions})")

                # ── Сессия, умершая на подключении, НЕ считается выполненной ──
                # Инцидент 26.08: executed_idx увеличивался после блока
                # обработки ошибок, поэтому сорванная на коннекте сессия
                # помечалась пройденной и больше не повторялась. За сутки так
                # потерялось 109 сессий: в исполненных лежало 183 коммента,
                # а попыток отправки было всего 23.
                # Не двигаем указатель — диспетчер попробует ту же сессию
                # снова. Вечного цикла не будет: просроченные больше чем на
                # 4 часа сессии отбрасываются проверкой в dispatch_plans.
                if _session_never_started:
                    # ── Отступ после подряд идущих обрывов ────────────────
                    # Указатель не двигаем — сессия должна повториться. Но
                    # повторять её каждые несколько минут бессмысленно: если
                    # прокси лёг, он лежит часами. Замер 01.09: acc#208
                    # стартовал 43 раза за сутки при плане в пять, и так же
                    # вели себя все аккаунты двух упавших адресов — 252
                    # пустые сессии за три часа.
                    # После трёх обрывов подряд аккаунт уходит на час.
                    # Счётчик живёт два часа и обнуляется удачной сессией.
                    _fails = 1
                    try:
                        from utils.redis_pool import get_redis as _g_cf
                        _r_cf = _g_cf()
                        _k_cf = f"gramgpt:conn_fail:{plan.account_id}"
                        _fails = int(_r_cf.incr(_k_cf))
                        _r_cf.expire(_k_cf, 7200)
                    except Exception:
                        pass
                    if _fails >= 3:
                        from utils.account_health import set_rest as _sr_cf
                        _sr_cf(plan.account_id, 3600,
                               f"не подключается: {_fails} обрыва подряд")
                        logger.warning(
                            f"[plan][{phone}] {_fails} обрыва подряд — час отдыха, "
                            f"хватит долбить мёртвый адрес"
                        )
                    else:
                        logger.warning(
                            f"[plan][{phone}] сессия не состоялась (обрыв связи, "
                            f"{_fails}-й подряд) — оставляем в очереди"
                        )
                    _release_session(plan_id, _claim_idx)
                    await db.commit()
                    return {"status": "connect_failed", "account": phone,
                            "fails": _fails}

                # Сессия дошла до конца — связь есть, счётчик обрывов сбрасываем.
                try:
                    from utils.redis_pool import get_redis as _g_ok
                    _g_ok().delete(f"gramgpt:conn_fail:{plan.account_id}")
                except Exception:
                    pass

                _release_session(plan_id, _claim_idx)
                plan.executed_idx += 1
                if plan.executed_idx >= len(sessions):
                    plan.status = "done"

                await db.commit()
                return {"status": "done", "actions": done_actions, "account": phone}

            finally:
                release_account_lock(plan.account_id)
                if slot_user_id is not None:
                    release_user_slot(slot_user_id)

        except Exception as e:
            logger.error(f"[plan_executor] #{plan_id}: {e}")
            try:
                await db.rollback()
            except:
                pass
            return {"error": str(e)}


# ═══════════════════════════════════════════════════════════
# CELERY TASKS
# ═══════════════════════════════════════════════════════════

@celery_app.task(bind=True, name="tasks.plan_executor.dispatch_plans",
                 acks_late=False, reject_on_worker_lost=False)
def dispatch_plans(self):
    """Диспетчер планов (<1с)."""
    return run_async(_dispatch_plans())


@celery_app.task(bind=True, name="tasks.plan_executor.execute_plan_session",
                 acks_late=False, reject_on_worker_lost=False)
def execute_plan_session(self, plan_id: int):
    """Одна сессия одного аккаунта — параллельно."""
    return run_async(_execute_plan_session(plan_id))