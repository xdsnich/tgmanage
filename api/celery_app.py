"""
GramGPT API — celery_app.py
"""
import sys
import os

# ── ВАЖНО: sys.path-cleanup ДОЛЖЕН быть до любого 'from config import ...' ──
# В корне репо лежит легаси tg_manager1/config.py без DATABASE_URL.
# Если PYTHONPATH/IDE/.pth кладёт parent dir в sys.path раньше api/,
# Python берёт легаси и таски падают на импорте database.py.
_API_DIR    = os.path.dirname(os.path.abspath(__file__))
_PARENT_DIR = os.path.dirname(_API_DIR)
sys.path[:] = [p for p in sys.path
               if os.path.normcase(os.path.abspath(p) if p else os.getcwd()) != os.path.normcase(_PARENT_DIR)]
if _API_DIR not in sys.path:
    sys.path.insert(0, _API_DIR)
# Сбрасываем кеш модулей если config уже был импортирован из неправильного места
for _mod in list(sys.modules):
    if _mod == "config" or _mod.startswith("config."):
        del sys.modules[_mod]

# Windows console может иметь не-UTF-8 кодировку — принудительно переключаем
if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

from celery import Celery
from celery.schedules import crontab
from celery.signals import worker_process_init, beat_init
import os
from dotenv import load_dotenv

load_dotenv()
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# ── Logging с ротацией ──────────────────────────────────────
# Подключаем при старте процесса (worker или beat).
# logs/celery.log + logs/beat.log с ротацией 100MB × 10 файлов.
@worker_process_init.connect
def _setup_worker_logging(**_):
    from utils.logging_setup import setup_logging
    setup_logging("celery")



@beat_init.connect
def _setup_beat_logging(**_):
    from utils.logging_setup import setup_logging
    setup_logging("beat")

celery_app = Celery(
    "gramgpt",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=[
        "tasks.account_tasks",
        "tasks.proxy_tasks",
        "tasks.bulk_tasks",
        "tasks.ai_tasks",
        "tasks.commenting_tasks",
        "tasks.comment_executor",
        "tasks.realtime_comments",   # ← Комменты по факту выхода поста
        "tasks.promote_checked",     # ← Проверенные каналы → в гео-папки
        "tasks.rt_restore",          # ← Возврат ограниченных, когда срок вышел
        "tasks.funnel_snapshot",     # ← Полуночный снимок воронки (приток за сутки)
        "tasks.warmup_tasks",
        "tasks.warmup_v2",
        "tasks.subscribe_tasks",
        "tasks.react_subscribe",     # ← Чистые аккаунты в каналы «только реакции»
        "tasks.plan_executor",
        "tasks.parser_tasks",
        "tasks.parser_similar_tasks",   # ← ДОБАВЛЕНО: crawler похожих каналов
        "tasks.parser_multi_tasks",     # ← Мульти-акк парсер с random concurrency
        "tasks.parser_similar_multi_tasks",  # ← Мульти-акк паутинка (crawler)
        "tasks.backfill_discussions",   # ← Догнать discussion-подписки постфактум
        "tasks.subscription_discovery", # ← Сканирование существующих подписок акка (anti-ban)
        "tasks.web_scraper_tasks",      # ← ДОБАВЛЕНО: Camoufox web scraper
        "tasks.leomatch_onboarding",    # ← Массовый онбординг в @leomatchbot
        "tasks.channel_probe",          # ← Свойства каналов (2 read-only запроса)
        "tasks.bulk_channel_check",     # ← Массовая проверка каналов по прокси
        "tasks.quarantine_probe",       # ← Проверка каналов карантинными аккаунтами
    ]
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,

    task_routes={
        # Быстрые юзер-инициированные операции
        "tasks.account_tasks.*":         {"queue": "high_priority"},
        "tasks.proxy_tasks.*":           {"queue": "high_priority"},

        # Bulk-операции (импорт TData, 2FA batch и т.п.)
        "tasks.bulk_tasks.*":            {"queue": "bulk_actions"},
        "tasks.backfill_discussions.*":  {"queue": "bulk_actions"},

        # ПЛАНЫ КАМПАНИЙ — самые важные, отдельная очередь
        # для предсказуемой latency. Дальше всё масштабируется по ней.
        "tasks.plan_executor.*":         {"queue": "plans"},

        # Прогрев аккаунтов — отдельная очередь
        "tasks.warmup_v2.*":             {"queue": "warmup"},
        "tasks.warmup_tasks.*":          {"queue": "warmup"},

        # Парсеры — могут быть долгими (crawler до 10 мин), изолируем
        # чтобы не блокировали planning/commenting
        "tasks.parser_tasks.*":          {"queue": "parsers"},
        "tasks.parser_similar_tasks.*":  {"queue": "parsers"},
        "tasks.parser_multi_tasks.*":    {"queue": "parsers"},
        "tasks.parser_similar_multi_tasks.*": {"queue": "parsers"},
        "tasks.web_scraper_tasks.*":     {"queue": "parsers"},
        "tasks.channel_probe.*":         {"queue": "parsers"},
        "tasks.bulk_channel_check.*":    {"queue": "parsers"},
        "tasks.quarantine_probe.*":      {"queue": "parsers"},

        # Подписки — отдельно, бывают long-running
        "tasks.subscribe_tasks.*":       {"queue": "subscribe"},
        "tasks.react_subscribe.*":       {"queue": "subscribe"},

        # AI-диалоги и комментинг
        "tasks.ai_tasks.*":              {"queue": "ai_dialogs"},
        "tasks.commenting_tasks.*":      {"queue": "ai_dialogs"},
        "tasks.comment_executor.*":      {"queue": "ai_dialogs"},
        "tasks.realtime_comments.*":     {"queue": "ai_dialogs"},
        "tasks.promote_checked.*":       {"queue": "parsers"},

        # Массовый онбординг в @leomatchbot — batch может идти 20-40 мин
        "tasks.leomatch_onboarding.*":   {"queue": "bulk_actions"},
    },

    result_expires=3600,
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    task_track_started=True,

    # ── Тайм-ауты тасков ─────────────────────────────────────
    # Hard kill: таск убивается после 10 мин (защита от вечно висящих)
    task_time_limit=600,
    # Soft warning: SoftTimeLimitExceeded поднимается на 8-й минуте — можно почистить ресурсы
    task_soft_time_limit=480,

    # ── Быстрый shutdown ─────────────────────────────────────
    # При Ctrl+C ждём максимум 5 секунд, потом force-kill всех тредов.
    # Без этого Celery+threads на Windows может зависать на minutes пока
    # redis.brpop в C-level вернётся (треды нельзя прервать сигналом).
    worker_shutdown_timeout=5,

    # При потере коннекта к Redis — отменять долгие таски (а не висеть бесконечно)
    worker_cancel_long_running_tasks_on_connection_loss=True,

    # Не слать события задач в broker — экономит Redis-трафик и ускоряет shutdown
    # (на dev можно False, на prod = True если используешь Flower с full features)
    worker_send_task_events=True,
    task_send_sent_event=False,

    # Не держать результаты тасков в Redis дольше нужного
    task_ignore_result=False,

    # ── Beat schedule — заменяет run_periodic.py ─────────────
    # Beat = встроенный планировщик Celery с supervisor-friendly архитектурой.
    # Запуск: celery -A celery_app beat --loglevel=info
    # Beat НЕ выполняет таски сам — он только посылает их в очередь воркеру
    # по расписанию. Так что Beat и Worker — это два РАЗНЫХ процесса.
    #
    # ── Срок годности у задач расписания (expires) ───────────────
    # Все задачи ниже — повторяющиеся и одинаковые: пропущенный тик не
    # надо навёрстывать, через минуту придёт следующий и сделает ровно
    # то же самое, только по свежим данным.
    #
    # Без срока годности пропущенные тики копятся в Redis и при запуске
    # воркера уходят залпом. Так было 09.09: Windows поставил обновления
    # и перезагрузил машину, воркер умер с полусотней разобранных, но не
    # подтверждённых задач; Redis вернул их в очередь, и на старте
    # диспетчер планов отработал 76 раз за две минуты вместо одного раза
    # в минуту. Каждый проход двигал указатель сессии вперёд по правилу
    # «просрочено больше 4 часов — пропускаем», и 37 сессий кампании
    # сгорели, ни разу не выполнившись. Наблюдатель realtime в тот же
    # момент набил очередь восемнадцатью заданиями при потолке в пять.
    # Тот же сценарий описан в шапке start_all.ps1 — инцидент 2026-06-12.
    #
    # expires чуть больше периода: тик, не начатый за это время, уже
    # неактуален и Celery выбросит его сам, не отдавая воркеру.
    beat_schedule={
        "dispatch-plans": {
            # Исполняет И commenting, И warmup планы (единый движок).
            "task": "tasks.plan_executor.dispatch_plans",
            "schedule": 60.0,                  # каждые 60 секунд
            "options": {"queue": "plans", "expires": 90},
        },
        # ── ОТКЛЮЧЕНО 2026-08-24 ─────────────────────────────────
        # dispatch-channel-probe бил по 40 каналов на аккаунт каждые 30 мин,
        # то есть до 1920 проверок в сутки на аккаунт. Реальный безопасный
        # предел по ResolveUsername оказался около 150-200: замер 23.08 —
        # acc#210 шёл 49 проверок/час и словил FLOOD_WAIT на 5 часов, а
        # аккаунты на 8-33/час выжили при том же суточном объёме.
        # Заменено на tasks.bulk_channel_check.run_bulk_check — там темп
        # ограничен, аккаунты одного IP идут по очереди, разные IP параллельно.
        # Запускается вручную, не по расписанию.
        #
        # "dispatch-channel-probe": {
        #     "task": "tasks.channel_probe.dispatch_probe",
        #     "schedule": 1800.0,
        #     "options": {"queue": "parsers"},
        # },
        # ── Комменты по факту выхода поста ───────────────────────
        # Наблюдатель обходит каналы realtime-кампаний через публичную
        # веб-версию t.me/s/ — аккаунтов и их лимитов это не касается.
        # Замер 08.09: 4800 запросов подряд по 20 в секунду через один
        # прокси, ни одного 429. Так что минута между обходами — с
        # огромным запасом.
        #
        # Если realtime-кампаний нет, обе задачи выходят сразу и почти
        # ничего не стоят.
        "watch-realtime-channels": {
            "task": "tasks.realtime_comments.watch_channels",
            "schedule": 60.0,
            "options": {"queue": "ai_dialogs", "expires": 55},
        },
        # Ограничения за жалобы почти всегда временные — бот называет
        # дату снятия. Задача сама проверяет созревших через @SpamBot и
        # возвращает чистых в кампанию; раз в двадцать минут достаточно,
        # сроки измеряются сутками.
        # Полуночный снимок воронки. Список заявок читается постранично и
        # целиком не отдаётся, а счётчики канала — отдаются. Разница
        # между снимком и текущим значением и есть суточный приток.
        "funnel-midnight-snapshot": {
            "task": "tasks.funnel_snapshot.snapshot_funnel",
            # Расписание celery живёт в UTC, а нам нужна полночь по Киеву
            # (UTC+3) — это 21:02 предыдущих суток по UTC. Ставить 00:02
            # было бы промахом на три часа.
            "schedule": crontab(hour=21, minute=2),
            "options": {"queue": "ai_dialogs", "expires": 1800},
        },

        # Каналы «только реакции» живут, пока в них сидят аккаунты без
        # бана — только они и ставят там реакции. Раз в ночь добираем
        # недостающих, по десять вступлений на аккаунт, пачками по пять.
        # 22:15 UTC = 01:15 по Киеву. Выключить: RT_REACT_SUBSCRIBE=0.
        "react-channels-subscribe": {
            "task": "tasks.react_subscribe.plan_react_subscriptions",
            "schedule": crontab(hour=22, minute=15),
            "options": {"queue": "subscribe", "expires": 3600},
        },

        "restore-limited-accounts": {
            "task": "tasks.rt_restore.restore_limited",
            # Раз в десять минут, а не в двадцать: адреса заняты почти
            # всегда, и чем чаще заходим, тем выше шанс поймать окно.
            "schedule": 600.0,
            "options": {"queue": "ai_dialogs", "expires": 550},
        },

        "dispatch-realtime-comments": {
            "task": "tasks.realtime_comments.dispatch_realtime",
            "schedule": 30.0,
            "options": {"queue": "ai_dialogs", "expires": 28},
        },
        # Проверка каналов идёт пачками круглосуточно, и её результат
        # надо разбирать так же — иначе проверенное копится в отстойнике
        # и в работу не попадает. Было ровно так: 9303 канала проверены,
        # 3159 пригодны, а счётчик в интерфейсе стоял неделю.
        "promote-checked-channels": {
            "task": "tasks.promote_checked.promote_checked",
            "schedule": 300.0,
            "options": {"queue": "parsers"},
        },
        "dispatch-prep-plan-sessions": {
            # Диспетчер prep-plan (⚙ Подготовка → 🗓 План).
            # Пере-выкидывает scheduled сессии с прошедшим connect_ts.
            # Нужно потому что Celery send_task(countdown=N) хранит
            # задачи в памяти воркера — на рестарте они теряются.
            "task": "tasks.bulk_tasks.dispatch_prep_plan_sessions",
            "schedule": 60.0,
            "options": {"queue": "bulk_actions"},
        },
        # dispatch-warmups ОТКЛЮЧЁН: родной warmup_v2-движок больше не исполняет
        # прогрев. Прогрев теперь гоняет plan_executor (см. dispatch-plans),
        # чтобы план ВСЕГДА совпадал с выполнением (был баг двойного движка).
        #
        # process-ai-dialogs ОТКЛЮЧЁН (по запросу 2026-06-13):
        # пока идёт стабилизация после инцидента с потерей 200 акков —
        # не дёргаем AI-сценарии вообще. Включить обратно: вернуть блок.
        # "process-ai-dialogs": {
        #     "task": "tasks.ai_tasks.process_ai_dialogs",
        #     "schedule": 60.0,
        #     "options": {"queue": "ai_dialogs"},
        # },
    },
)


# ── Карта моделей собирается здесь, до первой задачи ─────────────
# Воркер запущен в 40 потоков (-P threads), и раньше карта складывалась
# сама собой — тем импортом, который случился первым. Пока один поток
# исполнял models/__init__, другой видел пакет наполовину собранным,
# брал из него нужную модель и получал мапперы без соседей:
#   expression 'ApiApp' failed to locate a name
#   expression 'Proxy' failed to locate a name
# Валилось всё, что трогает аккаунты, причём плавающе — в зависимости от
# того, какая задача успела первой.
#
# Делать это в worker_process_init нельзя: тот сигнал приходит только у
# процессного пула, а у потокового не приходит вовсе — проверено 09.09,
# хук молчал, а мапперы падали дальше. На импорте модуля выполнится
# гарантированно и ровно один раз, до приёма задач.
try:
    import models  # noqa: F401  — регистрирует ВСЕ модели разом
    from sqlalchemy.orm import configure_mappers as _configure_mappers
    _configure_mappers()
except Exception:  # pragma: no cover
    import logging as _logging
    _logging.getLogger(__name__).exception(
        "[celery] не удалось собрать карту моделей на старте")

# Движок базы — здесь же, по той же причине. Сам по себе он импортируется
# лениво, из первой задачи, а задача уже крутится в своём цикле событий:
# первое подключение берёт внутренний замок asyncio, и соседняя задача из
# другого цикла падает на нём с «Lock is bound to a different event loop».
# При каждом перезапуске так гибли две первые задачи. Импорт отсюда —
# из главного потока, где никакого цикла ещё нет, — даёт прогреву
# сработать и снимает гонку.
try:
    import utils.db_pool  # noqa: F401
except Exception:  # pragma: no cover
    import logging as _logging
    _logging.getLogger(__name__).exception(
        "[celery] движок базы не прогрелся на старте")
