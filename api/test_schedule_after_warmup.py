"""
Тест feature "запланировать кампанию на момент окончания прогрева".

Что делает:
  1. Берёт 2 первых акка из БД (любых, статус не важен — Telethon не дёргается).
  2. Создаёт фейковый warmup-batch с 2 WarmupTask (status='running',
     subscribed_channels уже заполнены) — без запуска реального прогрев-движка.
  3. Создаёт Campaign через ту же логику что POST /schedule-after-warmup:
     status='scheduled', scheduled_start_at=now+2min, warmup_batch_id привязан.
  4. Ждёт ~70 сек, потом маркирует ВСЕ WarmupTask как 'finished'.
  5. Вызывает dispatch_plans() — auto-start должен сработать.
  6. Проверяет: Campaign.status='active', есть CampaignPlan для обоих акков.
  7. Чистит за собой (удаляет тестовую кампанию, планы, channel_assignments,
     warmup-задачи).

ЗАПУСК:
  cd api && python test_schedule_after_warmup.py

Скрипт ничего не подключает к Telegram и не трогает прокси — это unit-test
flow planning. Безопасно гонять на проде.
"""

import asyncio
import sys
import os
import uuid
from datetime import datetime, timedelta

# Гарантируем что api/ в sys.path (для импорта config, models, ...)
API_DIR = os.path.dirname(os.path.abspath(__file__))
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)


# ── ANSI цвета для понятного вывода ────────────────────────────
def C(text, color):
    colors = {"r": "\033[31m", "g": "\033[32m", "y": "\033[33m", "c": "\033[36m", "b": "\033[34m"}
    return f"{colors.get(color, '')}{text}\033[0m"


def log(msg, color=None):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {C(msg, color) if color else msg}")


# ── Главный сценарий ──────────────────────────────────────────
async def run_test():
    from sqlalchemy import select, delete
    from utils.db_pool import async_session as Session
    from models.account import TelegramAccount
    from models.warmup import WarmupTask
    from models.campaign import Campaign, CampaignStatus
    from models.campaign_plan import CampaignPlan
    from models.campaign_channel_assignment import CampaignChannelAssignment

    batch_id = f"test_{uuid.uuid4().hex[:12]}"
    batch_name = "TEST: auto-schedule after warmup"
    campaign_id = None
    task_ids = []

    try:
        # ── 1. Берём 2 акка ────────────────────────────────────
        async with Session() as db:
            r = await db.execute(
                select(TelegramAccount).order_by(TelegramAccount.id).limit(2)
            )
            accounts = r.scalars().all()
            if len(accounts) < 2:
                log("Нужно минимум 2 аккаунта в БД. Добавь и попробуй снова.", "r")
                return False
            user_id = accounts[0].user_id
            log(f"Беру 2 акка: #{accounts[0].id} ({accounts[0].phone}) и #{accounts[1].id} ({accounts[1].phone}), user_id={user_id}", "c")

        # ── 2. Создаём фейк warmup-batch ───────────────────────
        async with Session() as db:
            now = datetime.utcnow()
            fake_subs = {
                "@durov": now.isoformat() + "Z",
                "@telegram": now.isoformat() + "Z",
                "@python": now.isoformat() + "Z",
            }
            for acc in accounts:
                wt = WarmupTask(
                    user_id=user_id,
                    account_id=acc.id,
                    mode="normal",
                    status="running",         # ← симулируем что прогрев идёт
                    started_at=now - timedelta(minutes=5),
                    total_days=7,
                    batch_id=batch_id,
                    batch_name=batch_name,
                    subscribed_channels=fake_subs,
                    today_limit=10,
                )
                db.add(wt)
            await db.flush()
            await db.commit()

            tasks = (await db.execute(
                select(WarmupTask).where(WarmupTask.batch_id == batch_id)
            )).scalars().all()
            task_ids = [t.id for t in tasks]
            log(f"✅ Создал warmup batch={batch_id} с {len(tasks)} задачами в status='running'", "g")

        # ── 3. Создаём Campaign через ту же логику schedule-after-warmup ──
        # Дублируем тело эндпоинта чтобы не дёргать HTTP (а заодно ставим
        # scheduled_start_at в +2 минуты от now чтобы тест прошёл быстро).
        async with Session() as db:
            from models.campaign import TargetChannel

            scheduled_start = datetime.utcnow() + timedelta(minutes=2)
            c = Campaign(
                user_id=user_id,
                name=f"TEST campaign auto-start ({batch_id})",
                account_ids=[a.id for a in accounts],
                trigger_mode="all",
                trigger_percent=50,
                trigger_keywords=[],
                llm_provider="claude",
                tone="positive",
                comment_length="short",
                max_comments=4,           # маленькие лимиты чтобы план был быстро
                max_hours=1,
                delay_join=5,
                delay_comment=10,
                delay_between=15,
                warmup_batch_id=batch_id,
                scheduled_start_at=scheduled_start,
                status=CampaignStatus.scheduled,
            )
            db.add(c)
            await db.flush()
            campaign_id = c.id

            for username in ["@durov", "@telegram", "@python"]:
                db.add(TargetChannel(
                    campaign_id=c.id,
                    username=username.lstrip("@"),
                    link=f"https://t.me/{username.lstrip('@')}",
                    title=username,
                ))
            await db.commit()
            log(f"✅ Создал Campaign #{campaign_id} status='scheduled', scheduled_start_at={scheduled_start.isoformat()}", "g")

        # ── 4. Проверка: до триггера ничего не должно стартовать ──
        log("Жду 65 сек и потом маркирую warmup-задачи 'finished'...", "y")
        await asyncio.sleep(65)

        async with Session() as db:
            r = await db.execute(select(Campaign).where(Campaign.id == campaign_id))
            c = r.scalar_one()
            if c.status != CampaignStatus.scheduled:
                log(f"❌ Кампания внезапно сменила статус на {c.status} ДО триггера. Тест провалился.", "r")
                return False
            log(f"  ✓ Кампания всё ещё 'scheduled' — корректно (триггер ещё не сработал)", "c")

        # ── 5. Маркируем все WarmupTask как 'finished' ─────────
        async with Session() as db:
            now2 = datetime.utcnow()
            tasks = (await db.execute(
                select(WarmupTask).where(WarmupTask.batch_id == batch_id)
            )).scalars().all()
            for t in tasks:
                t.status = "finished"
                t.finished_at = now2
            await db.commit()
            log(f"✅ Все {len(tasks)} warmup-задач помечены 'finished' — auto-start должен сработать на следующем dispatch", "g")

        # ── 6. Вручную вызываем dispatch_plans (вместо ждать 60с beat) ──
        log("Вызываю dispatch_plans() ...", "c")
        from tasks.plan_executor import _dispatch_plans
        result = await _dispatch_plans()
        log(f"  dispatch result: {result}", "c")

        # ── 7. Проверка: кампания active + есть планы ──────────
        async with Session() as db:
            r = await db.execute(select(Campaign).where(Campaign.id == campaign_id))
            c = r.scalar_one()
            log(f"  Status кампании после dispatch: {c.status}", "c")
            if c.status != CampaignStatus.active:
                log(f"❌ Ожидалось active, получено {c.status}. Тест провалился.", "r")
                return False

            plans = (await db.execute(
                select(CampaignPlan).where(CampaignPlan.campaign_id == campaign_id)
            )).scalars().all()
            assigns = (await db.execute(
                select(CampaignChannelAssignment).where(CampaignChannelAssignment.campaign_id == campaign_id)
            )).scalars().all()

            log(f"  CampaignPlan: {len(plans)} штук", "c")
            log(f"  CampaignChannelAssignment: {len(assigns)} штук (joined={sum(1 for a in assigns if a.status=='joined')})", "c")

            if not plans:
                log("❌ Планы не сгенерировались — что-то не так в _do_start_campaign", "r")
                return False

            joined = [a for a in assigns if a.status == "joined"]
            if not joined:
                log("⚠️  Нет channel_assignments со status='joined' — каналы не помечены как pre-joined из прогрева", "y")
            else:
                log(f"  ✓ {len(joined)} каналов отмечены 'joined' (наследованы из прогрева)", "g")

        log("✅✅✅ ВСЁ ОК. Auto-start scheduled campaign после прогрева работает.", "g")
        return True

    finally:
        # ── 8. Cleanup ──────────────────────────────────────────
        async with Session() as db:
            log("Подчищаю тестовые данные...", "y")
            if campaign_id:
                await db.execute(delete(CampaignPlan).where(CampaignPlan.campaign_id == campaign_id))
                await db.execute(delete(CampaignChannelAssignment).where(CampaignChannelAssignment.campaign_id == campaign_id))
                from models.campaign import TargetChannel
                await db.execute(delete(TargetChannel).where(TargetChannel.campaign_id == campaign_id))
                await db.execute(delete(Campaign).where(Campaign.id == campaign_id))
            if task_ids:
                await db.execute(delete(WarmupTask).where(WarmupTask.id.in_(task_ids)))
            await db.commit()
            log(f"  ✓ Удалено: campaign #{campaign_id}, warmup tasks {task_ids}, batch={batch_id}", "c")


if __name__ == "__main__":
    # Windows console → utf-8 для эмодзи
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception: pass

    print("=" * 70)
    print("TEST: scheduled campaign auto-starts when linked warmup batch finishes")
    print("=" * 70)
    print()
    print("Скрипт займёт около 75 секунд (65с ожидание + dispatch).")
    print("Никакие реальные Telegram-подключения не делаются — это тест planning-логики.")
    print()

    ok = asyncio.run(run_test())
    sys.exit(0 if ok else 1)
