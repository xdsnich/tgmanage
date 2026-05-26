"""
GramGPT — test_smoke.py
Smoke test всей системы — за 5 секунд проверяет что ничего не сломано.

Что проверяет:
  1. Все модули импортируются (нет circular imports, нет битых файлов)
  2. БД доступна (через db_pool)
  3. Redis доступен (через redis_pool)
  4. Все миграции применены (в т.ч. 024, 025)
  5. Критичные индексы существуют (из migration 025)
  6. Celery broker отвечает
  7. Конфиг celery_app загружается (task_routes, task_time_limit)
  8. Hot-path запросы быстрые (план-диспатчер, smart_comment guard)

Запуск:
  cd api
  python test_smoke.py

Используй ПОСЛЕ каждого деплоя или больших изменений.
Если что-то красное — не запускай прод-воркер пока не починишь.
"""

import sys
import os
import asyncio
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class C:
    R = "\033[31m"; G = "\033[32m"; Y = "\033[33m"; B = "\033[34m"
    C = "\033[36m"; M = "\033[35m"; BOLD = "\033[1m"; DIM = "\033[2m"; OFF = "\033[0m"


checks_passed = 0
checks_failed = 0


def check(label):
    """Decorator that wraps a check function with timing + status."""
    def deco(fn):
        async def wrapper():
            global checks_passed, checks_failed
            t0 = time.time()
            try:
                detail = await fn()
                elapsed = (time.time() - t0) * 1000
                print(f"  {C.G}✓{C.OFF} {label}{C.DIM} — {detail} ({elapsed:.0f}ms){C.OFF}")
                checks_passed += 1
                return True
            except Exception as e:
                elapsed = (time.time() - t0) * 1000
                print(f"  {C.R}✕{C.OFF} {label}{C.DIM} ({elapsed:.0f}ms){C.OFF}")
                print(f"    {C.R}{type(e).__name__}:{C.OFF} {str(e)[:200]}")
                checks_failed += 1
                return False
        return wrapper
    return deco


# ─────────────────────────────────────────────────────────
# Проверки
# ─────────────────────────────────────────────────────────

@check("Импорт всех task-модулей")
async def check_imports():
    modules = [
        "tasks.plan_executor",
        "tasks.warmup_v2",
        "tasks.warmup_tasks",
        "tasks.parser_tasks",
        "tasks.parser_similar_tasks",
        "tasks.commenting_tasks",
        "tasks.comment_executor",
        "tasks.subscribe_tasks",
        "tasks.ai_tasks",
        "tasks.bulk_tasks",
        "tasks.account_tasks",
        "utils.db_pool",
        "utils.redis_pool",
        "utils.user_lock",
        "utils.account_lock",
        "utils.connection_limiter",
    ]
    import importlib
    for m in modules:
        importlib.import_module(m)
    return f"{len(modules)} модулей"


@check("DB pool (NullPool, asyncpg)")
async def check_db():
    from utils.db_pool import async_session as Session
    from sqlalchemy import text
    async with Session() as db:
        result = await db.execute(text("SELECT 1"))
        if result.scalar() != 1:
            raise RuntimeError("SELECT 1 не вернул 1")
    return "connect + SELECT 1 OK"


@check("Redis pool")
async def check_redis():
    from utils.redis_pool import get_redis
    r = get_redis()
    r.ping()
    # smoke-test acquire/release
    r.set("gramgpt:smoke", "1", ex=10)
    v = r.get("gramgpt:smoke")
    r.delete("gramgpt:smoke")
    if v != b"1":
        raise RuntimeError(f"set/get mismatch: {v}")
    return "PING + SET/GET/DEL"


@check("Все миграции применены")
async def check_migrations():
    from utils.db_pool import async_session as Session
    from sqlalchemy import text
    import glob

    # Считаем файлы миграций
    mig_dir = os.path.join(os.path.dirname(__file__), "migrations")
    files = sorted(glob.glob(os.path.join(mig_dir, "[0-9]*.py")))
    expected = [os.path.basename(f).replace(".py", "") for f in files]

    async with Session() as db:
        result = await db.execute(text("SELECT migration FROM _migrations"))
        applied = {row[0] for row in result.fetchall()}

    missing = [m for m in expected if m not in applied]
    if missing:
        raise RuntimeError(f"не применены: {missing}")

    return f"{len(applied)}/{len(expected)} ({', '.join(sorted(expected)[-3:])} — последние)"


@check("Критичные индексы из migration 025")
async def check_indexes():
    from utils.db_pool import async_session as Session
    from sqlalchemy import text

    expected = [
        "ix_cp_plandate_status",
        "ix_cp_campaign_status",
        "ix_campaigns_status",
        "ix_cca_camp_acc_ch_status",
        "ix_comment_logs_camp_created",
        "ix_warmup_logs_task_created",
        "ix_accounts_user_status",
    ]

    async with Session() as db:
        result = await db.execute(text("""
            SELECT indexname FROM pg_indexes
            WHERE indexname = ANY(:names)
        """), {"names": expected})
        found = {row[0] for row in result.fetchall()}

    missing = [i for i in expected if i not in found]
    if missing:
        raise RuntimeError(f"индексы не созданы (нужна migration 025?): {missing}")

    return f"{len(found)}/{len(expected)} ключевых индексов"


@check("Celery broker (Redis)")
async def check_celery():
    from celery_app import celery_app
    # Проверяем что broker доступен через inspect
    inspect = celery_app.control.inspect(timeout=2)
    # ping вернёт {} если никто не подключён, или dict от воркеров
    # Если broker недоступен — выкинет исключение
    inspect.ping()
    return "broker отвечает"


@check("Celery конфиг загружен корректно")
async def check_celery_config():
    from celery_app import celery_app
    cfg = celery_app.conf
    issues = []
    if cfg.task_time_limit is None or cfg.task_time_limit > 3600:
        issues.append("task_time_limit не настроен")
    if cfg.worker_shutdown_timeout is None or cfg.worker_shutdown_timeout > 60:
        issues.append("worker_shutdown_timeout не настроен")
    if "tasks.plan_executor.*" not in cfg.task_routes:
        issues.append("plans queue route отсутствует")
    if cfg.task_routes.get("tasks.plan_executor.*", {}).get("queue") != "plans":
        issues.append("plan_executor не маршрутится в 'plans'")
    if issues:
        raise RuntimeError(f"проблемы: {issues}")
    return f"time_limit={cfg.task_time_limit}s, shutdown={cfg.worker_shutdown_timeout}s, queues={len(set(v.get('queue') for v in cfg.task_routes.values()))}"


@check("Hot-path: запрос диспатчера планов")
async def check_dispatcher_query():
    from utils.db_pool import async_session as Session
    from sqlalchemy import text
    from datetime import datetime, timedelta

    today = (datetime.utcnow() + timedelta(hours=3)).date()

    async with Session() as db:
        result = await db.execute(text("""
            EXPLAIN (FORMAT JSON, BUFFERS, ANALYZE)
            SELECT * FROM campaign_plans
            WHERE plan_date = :d AND status = 'active'
            LIMIT 100
        """), {"d": today})
        plan = result.scalar()
        # plan — JSON, ищем Seq Scan
        plan_str = str(plan)
        if "Seq Scan" in plan_str and '"campaign_plans"' in plan_str:
            raise RuntimeError("используется Seq Scan вместо Index Scan! проверь migration 025")
    return "использует индекс (не Seq Scan)"


@check("Hot-path: smart_comment guard")
async def check_guard_query():
    from utils.db_pool import async_session as Session
    from sqlalchemy import text

    async with Session() as db:
        # Просто проверяем что запрос валидный (SELECT 0 строк ок)
        result = await db.execute(text("""
            EXPLAIN (FORMAT JSON)
            SELECT id FROM campaign_channel_assignments
            WHERE campaign_id = 1 AND account_id = 1
              AND channel_username = 'test' AND status = 'joined'
            LIMIT 1
        """))
        plan = str(result.scalar())
        # Точный композитный индекс должен использоваться
        if "Seq Scan" in plan:
            raise RuntimeError("guard использует Seq Scan! проверь ix_cca_camp_acc_ch_status")
    return "композитный индекс работает"


@check("user_lock acquire/release")
async def check_user_lock():
    from utils.user_lock import acquire_user_slot, release_user_slot, get_user_active
    # Используем синтетический user_id чтобы не задеть прода
    test_user = 99999999
    before = get_user_active(test_user)
    ok = acquire_user_slot(test_user, max_concurrent=100)
    if not ok:
        raise RuntimeError("acquire failed")
    middle = get_user_active(test_user)
    release_user_slot(test_user)
    after = get_user_active(test_user)
    if not (before <= after < middle):
        raise RuntimeError(f"некорректный счётчик: {before}→{middle}→{after}")
    return f"counter {before}→{middle}→{after}"


# ─────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────

async def main():
    print(f"\n{C.M}{C.BOLD}═══ GramGPT — SMOKE TEST ═══{C.OFF}\n")

    started = time.time()
    await check_imports()
    await check_db()
    await check_redis()
    await check_migrations()
    await check_indexes()
    await check_celery()
    await check_celery_config()
    await check_dispatcher_query()
    await check_guard_query()
    await check_user_lock()
    elapsed = time.time() - started

    print()
    total = checks_passed + checks_failed
    if checks_failed == 0:
        print(f"  {C.G}{C.BOLD}{checks_passed}/{total} OK{C.OFF}{C.DIM} за {elapsed:.2f}с{C.OFF}")
        print(f"  {C.G}✓ Система готова к запуску{C.OFF}\n")
        return 0
    else:
        print(f"  {C.R}{C.BOLD}{checks_failed} проверок упало из {total}{C.OFF}{C.DIM} за {elapsed:.2f}с{C.OFF}")
        print(f"  {C.R}✕ Не запускай воркеры пока не починишь{C.OFF}\n")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
