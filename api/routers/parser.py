"""
GramGPT API — routers/parser.py (v3)
Парсер каналов через Telegram-поиск (user accounts).
TGStat оставлен как опция, но по умолчанию используется Telegram.
"""

import sys
import os
import csv
import io
import asyncio
import logging
import random
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, func
from sqlalchemy.orm import joinedload

from database import get_db
from routers.deps import get_current_user
from models.user import User
from models.account import TelegramAccount
from models.proxy import Proxy
from models.parsed_channel import ParsedChannel

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/parser", tags=["parser"])


# ── Helper ───────────────────────────────────────────────────

async def _get_client(acc, db):
    api_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if api_dir not in sys.path:
        sys.path.insert(0, api_dir)
    from utils.telegram import make_telethon_client

    proxy = None
    if acc.proxy_id:
        proxy_r = await db.execute(select(Proxy).where(Proxy.id == acc.proxy_id))
        proxy = proxy_r.scalar_one_or_none()

    client = make_telethon_client(acc, proxy)
    if not client:
        raise HTTPException(status_code=400, detail="Файл сессии не найден")
    return client


# ── Schemas ──────────────────────────────────────────────────

class SearchRequest(BaseModel):
    account_id: int  # Обязательно для Telegram поиска

    # Ключевые слова (через запятую)
    keywords: str
    
    # Фильтры подписчиков
    min_subscribers: int = 0
    max_subscribers: int = 10000000

    # Только с открытыми комментариями
    only_with_comments: bool = True
    active_hours: int = 0  # Посты за последние N часов

    # Фильтры username
    name_endings: Optional[str] = None  # "_news,_info,_ua"
    name_contains: Optional[str] = None  # "crypto,trade"

    # Лимиты
    limit_per_keyword: int = 50
    max_channels: int = 500

    # Кастомные паузы (секунды)
    pause_between_keywords_min: float = 3.0
    pause_between_keywords_max: float = 6.0
    pause_between_channels_min: float = 0.8
    pause_between_channels_max: float = 1.5
    precise_check: bool = True
    flood_threshold: int = 300


class ImportRequest(BaseModel):
    channels: list[str]


# ── Endpoints ────────────────────────────────────────────────

@router.get("/channels")
async def list_parsed_channels(
    folder: Optional[str] = None,
    min_subscribers: Optional[int] = None,
    only_with_comments: bool = False,
    only_unchecked: bool = False,
    sort: str = "subs",
    limit: Optional[int] = None,
    offset: int = 0,
    response: Response = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Каналы пользователя. Отдаётся страницами.

    Раньше endpoint возвращал ВСЁ разом. При 35 тысячах каналов это
    мегабайты JSON на каждое открытие вкладки и на каждое автообновление
    раз в пятнадцать секунд — страница висла, а свежие находки паутинки
    появлялись только после полной перезагрузки.
    Теперь: limit/offset, а общее число едет в заголовке X-Total-Count,
    чтобы интерфейс знал, сколько ещё подгружать.
    """
    from models.channel_ban_stats import ChannelBanStats

    q = select(ParsedChannel).where(ParsedChannel.user_id == current_user.id)
    if folder is not None:
        q = q.where(ParsedChannel.folder == folder)
    if min_subscribers is not None:
        q = q.where(ParsedChannel.subscribers >= min_subscribers)
    if only_with_comments:
        # ── «С комментами» значит «можно писать», а не «есть обсуждение» ──
        # Раньше фильтр смотрел только на has_comments — флаг от парсера,
        # означающий наличие привязанной группы обсуждений. Замер 25.08:
        # из 4311 каналов с этим флагом 971 (23%) оказались непригодны —
        # 534 пускают только по заявке, 273 вообще без обсуждения, 99 с
        # запретом на запись. Такие каналы попадали в выборку и занимали
        # слоты комментариев, сгорая на исполнении.
        # Теперь опираемся на кэш свойств: показываем то, куда РЕАЛЬНО
        # можно писать. Непроверенные оставляем — они кандидаты, их ещё
        # предстоит проверить, и в UI они помечены отдельно.
        # Имена сравниваем без учёта регистра: вердикт мог записаться как
        # @nobyda, а в базе канал лежит как @NobyDa — 01.10 так терялись
        # вердикты у 3177 каналов.
        from models.channel_capability import ChannelCapability
        _bad = select(func.lower(ChannelCapability.channel_username)).where(
            ChannelCapability.commentable == False  # noqa: E712
        )
        q = q.where(
            ParsedChannel.has_comments == True,
            func.lower(ParsedChannel.username).notin_(_bad),
        )
    if only_unchecked:
        # «Непроверенные» — те, по кому вердикта ещё нет вовсе.
        # Раньше этот фильтр жил только в браузере и потому врал: он видел
        # лишь загруженный кусок списка, а не всю базу.
        from models.channel_capability import ChannelCapability as _CC
        q = q.where(func.lower(ParsedChannel.username).notin_(
            select(func.lower(_CC.channel_username))))
    # По умолчанию — крупные сверху. Но паутинка приносит каналы от 50
    # тысяч, а на первой странице по размеру стоят от 420 тысяч: свежие
    # находки не видно вовсе, пока не долистаешь. Поэтому есть второй
    # порядок — по времени находки.
    q = (q.order_by(ParsedChannel.added_at.desc().nullslast())
         if sort == "new" else q.order_by(ParsedChannel.subscribers.desc()))

    # Считаем общее число до нарезки — интерфейсу нужно знать, сколько всего.
    total = (await db.execute(
        select(func.count()).select_from(q.subquery()))).scalar() or 0

    if limit:
        q = q.offset(max(0, offset)).limit(min(int(limit), 5000))

    result = await db.execute(q)
    channels = result.scalars().all()

    # ── Справочники к выданной странице ──────────────────────────
    # Три таблицы дополняют каналы: проходимость, живучесть комментов и
    # вердикт проверки. Раньше каждая читалась ЦЕЛИКОМ и сопоставлялась в
    # памяти — так было сделано ради страховки от лимита Postgres в 32767
    # параметров запроса, который выбивал вкладку с InterfaceError, когда
    # в IN(...) уходили все 45 тысяч имён.
    # Теперь наружу уходит страница в сотни строк, и имена спокойно
    # помещаются в запрос: фильтруем на стороне базы. Разница ощутимая —
    # одни только channel_capabilities это 30 тысяч ORM-объектов на каждый
    # запрос, из-за них даже страница в десять каналов собиралась три
    # секунды. Полное чтение оставлено запасным путём: без limit имён
    # слишком много для IN(...).
    usernames = [c.username for c in channels if c.username]
    _want = set(usernames)
    # Ниже порога подставляем имена в запрос, выше — читаем таблицу целиком.
    _by_page = 0 < len(usernames) <= 5000
    stats_map = {}
    if usernames:
        _sq = select(ChannelBanStats).where(
            ChannelBanStats.user_id == current_user.id,
        )
        if _by_page:
            _sq = _sq.where(ChannelBanStats.channel_username.in_(usernames))
        stats_r = await db.execute(_sq)
        for s in stats_r.scalars().all():
            if s.channel_username not in _want:
                continue
            _sk = s.skipped_count or 0
            _bn = s.banned_count or 0
            pass_rate = 100.0 if s.total_attempts == 0 else round(
                max(0, s.total_attempts - _bn - _sk) / s.total_attempts * 100, 1
            )
            stats_map[s.channel_username] = {
                "pass_rate": pass_rate,
                "total_attempts": s.total_attempts,
                "banned_count": _bn,
                "skipped_count": _sk,
                "last_skip_reason": s.last_skip_reason or "",
            }

    # ── Живучесть комментов по каналу (2026-08-27) ───────────────
    # Раньше здесь было видно только «дошёл ли коммент до отправки»
    # (pass_rate). Но отправить — половина дела: часть каналов молча
    # удаляет комменты модерацией через минуты. Это знание уже копилось
    # в comment_logs.verify_status (его проставляет действие
    # verify_my_comments), просто никем не сводилось по каналам и в UI
    # не попадало. Сводим здесь — таблица маленькая (тысячи строк),
    # группировка дешевле, чем тащить её целиком в память.
    #   ok      — коммент на месте при повторной проверке
    #   deleted — исчез, канал его снял
    #   unknown — не смогли долистать до него, вердикта нет
    verify_map = {}
    try:
        from models.campaign import CommentLog, Campaign
        _vr = await db.execute(
            select(
                CommentLog.channel_username,
                func.count().label("written"),
                func.count().filter(CommentLog.verify_status == "ok").label("alive"),
                func.count().filter(CommentLog.verify_status == "deleted").label("dead"),
            )
            .join(Campaign, Campaign.id == CommentLog.campaign_id)
            .where(
                Campaign.user_id == current_user.id,
                *([CommentLog.channel_username.in_(usernames)] if _by_page else []),
            )
            .group_by(CommentLog.channel_username)
        )
        for uname, written, alive, dead in _vr.all():
            if uname not in _want:
                continue
            judged = (alive or 0) + (dead or 0)
            verify_map[uname] = {
                "comments_written": written or 0,
                "comments_alive": alive or 0,
                "comments_deleted": dead or 0,
                # None, пока ни один коммент этого канала не проверен —
                # ноль здесь читался бы как «всё удаляют», а это не так.
                "survival_rate": (round((alive or 0) / judged * 100, 1)
                                  if judged else None),
            }
    except Exception:
        verify_map = {}

    # ── Статус проверки канала (2026-08-24) ──────────────────────
    # has_comments — булево, и «проверен, комментов нет» в нём неотличимо
    # от «ещё не проверяли». Из-за этого в UI фильтр «только непроверенные»
    # показывал не то. Берём реальный статус из channel_capabilities:
    #   checked=False           → канал вообще не проверяли
    #   checked=True, cap_ok    → можно комментировать
    #   checked=True, !cap_ok   → нельзя, причина в cap_reason
    caps_map = {}
    if usernames:
        try:
            from models.channel_capability import ChannelCapability
            # Без учёта регистра — см. фильтры выше.
            _want_low = {u.lower() for u in _want}
            _cq = select(ChannelCapability)
            if _by_page:
                _cq = _cq.where(func.lower(ChannelCapability.channel_username).in_(list(_want_low)))
            caps_r = await db.execute(_cq)
            for cc in caps_r.scalars().all():
                if (cc.channel_username or "").lower() not in _want_low:
                    continue
                caps_map[cc.channel_username.lower()] = {
                    "reason": cc.reason or "",
                    "commentable": cc.commentable,
                    "needs_approval": cc.needs_approval,
                    "join_to_send": cc.join_to_send,
                    "checked_at": cc.checked_at.isoformat() if cc.checked_at else None,
                }
        except Exception:
            caps_map = {}

    if response is not None:
        response.headers["X-Total-Count"] = str(total)
        response.headers["Access-Control-Expose-Headers"] = "X-Total-Count"

    return [{
        "id": c.id,
        "username": c.username or "",
        "title": c.title or "",
        "language": getattr(c, 'language', '') or "",
        "subscribers": c.subscribers or 0,
        "has_comments": bool(c.has_comments),
        "last_post_date": c.last_post_date.isoformat() if c.last_post_date else None,
        "last_verification": getattr(c, 'last_verification').isoformat() if getattr(c, 'last_verification', None) else None,
        "search_query": c.search_query or "",
        "added_at": c.added_at.isoformat() if c.added_at else None,
        "folder": getattr(c, 'folder', '') or "",
        "country": getattr(c, 'country', '') or "",
        "language": getattr(c, 'language', '') or "",
        "category": getattr(c, 'category', '') or "",
        "description": (getattr(c, 'description', '') or "")[:200],
        "avg_post_reach": getattr(c, 'avg_post_reach', 0) or 0,
        "err": getattr(c, 'err', 0) or 0,
        "source": getattr(c, 'source', 'telegram') or "telegram",
        # Проходимость (из ChannelBanStats)
        "pass_rate": stats_map.get(c.username, {}).get("pass_rate"),  # None если нет статы
        "total_attempts": stats_map.get(c.username, {}).get("total_attempts", 0),
        "banned_count": stats_map.get(c.username, {}).get("banned_count", 0),
        "skipped_count": stats_map.get(c.username, {}).get("skipped_count", 0),
        "last_skip_reason": stats_map.get(c.username, {}).get("last_skip_reason", ""),
        # Живучесть (из CommentLog.verify_status)
        "comments_written": verify_map.get(c.username, {}).get("comments_written", 0),
        "comments_alive":   verify_map.get(c.username, {}).get("comments_alive", 0),
        "comments_deleted": verify_map.get(c.username, {}).get("comments_deleted", 0),
        "survival_rate":    verify_map.get(c.username, {}).get("survival_rate"),
        # Статус проверки на комментарии
        "checked": (c.username or "").lower() in caps_map,
        "cap_commentable": caps_map.get((c.username or "").lower(), {}).get("commentable"),
        "cap_reason": caps_map.get((c.username or "").lower(), {}).get("reason", ""),
        "cap_needs_approval": caps_map.get((c.username or "").lower(), {}).get("needs_approval"),
        "cap_join_to_send": caps_map.get((c.username or "").lower(), {}).get("join_to_send"),
        "cap_checked_at": caps_map.get((c.username or "").lower(), {}).get("checked_at"),
    } for c in channels]

@router.get("/channels/stats")
async def parsed_channels_stats(
    folder: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Сводка по всей базе каналов: сколько всего, с комментариями,
    непроверенных и средний размер.

    Нужна потому, что список теперь приезжает страницами. Карточки наверху
    считались по загруженному массиву и после перехода на подгрузку стали
    показывать «300 каналов» вместо тридцати пяти тысяч. Счёт по всей
    выборке умеет только база — четыре агрегата дешевле, чем выгрузка
    всех строк ради их подсчёта в браузере.
    """
    from models.channel_capability import ChannelCapability as _CC

    base = select(ParsedChannel).where(ParsedChannel.user_id == current_user.id)
    if folder is not None:
        base = base.where(ParsedChannel.folder == folder)

    async def _count(q):
        return (await db.execute(
            select(func.count()).select_from(q.subquery()))).scalar() or 0

    # Без учёта регистра: @NobyDa в базе и @nobyda в вердикте — один канал.
    _bad = select(func.lower(_CC.channel_username)).where(
        _CC.commentable == False)  # noqa: E712
    with_comments = base.where(
        ParsedChannel.has_comments == True,  # noqa: E712
        func.lower(ParsedChannel.username).notin_(_bad),
    )
    unchecked = base.where(
        func.lower(ParsedChannel.username).notin_(select(func.lower(_CC.channel_username))))

    # Считаем по колонке самого подзапроса: ParsedChannel.subscribers
    # снаружи не связан с алиасом и дал бы декартово произведение.
    _sub = base.subquery()
    avg_subs = (await db.execute(
        select(func.avg(_sub.c.subscribers)))).scalar()

    return {
        "total": await _count(base),
        "with_comments": await _count(with_comments),
        "unchecked": await _count(unchecked),
        "avg_subscribers": int(avg_subs or 0),
    }


@router.post("/search")
async def search_channels(
    body: SearchRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Запускает парсинг в фоне через Celery — возвращается сразу."""
    keywords = [k.strip() for k in body.keywords.split(",") if k.strip()]
    if not keywords:
        raise HTTPException(status_code=400, detail="Укажите хотя бы одно ключевое слово")

    # Проверяем аккаунт
    acc_r = await db.execute(
        select(TelegramAccount)
        .where(TelegramAccount.id == body.account_id, TelegramAccount.user_id == current_user.id)
    )
    acc = acc_r.scalar_one_or_none()
    if not acc:
        raise HTTPException(status_code=404, detail="Аккаунт не найден")
    if not acc.proxy_id:
        raise HTTPException(status_code=400, detail="У аккаунта нет прокси")

    # Отправляем в Celery
    from celery_app import celery_app
    task = celery_app.send_task(
        "tasks.parser_tasks.run_parser_search",
        args=[current_user.id, body.account_id, body.model_dump()],
        queue="ai_dialogs",  # или bulk_actions
    )

    return {
        "task_id": task.id,
        "status": "started",
        "message": "Парсинг запущен в фоне. Каналы появятся в списке по мере нахождения."
    }

# ── Удаление, экспорт, импорт ───────────────────────────────

@router.delete("/channels/{channel_id}", status_code=204)
async def delete_parsed_channel(
    channel_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ParsedChannel).where(ParsedChannel.id == channel_id, ParsedChannel.user_id == current_user.id)
    )
    ch = result.scalar_one_or_none()
    if ch:
        await db.delete(ch)
        await db.flush()


@router.delete("/channels", status_code=204)
async def clear_all_parsed(
    folder: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    q = delete(ParsedChannel).where(ParsedChannel.user_id == current_user.id)
    if folder is not None:
        q = q.where(ParsedChannel.folder == folder)
    await db.execute(q)
    await db.flush()


@router.get("/export")
async def export_channels_csv(
    folder: Optional[str] = None,               # "" = только каналы без папки, None = все
    only_with_comments: bool = False,
    min_subscribers: int = 0,
    max_subscribers: Optional[int] = None,
    format: str = "csv",                        # csv | txt | txt_links
    source: str = "parsed",                     # parsed | tgstat_checked
    # ── Фильтры по проходимости (из ChannelBanStats) ──
    min_pass_rate: float = 0.0,                 # 0..100. Только каналы где ok/total >= X%
    no_skips: bool = False,                     # только каналы где skipped_count = 0
    min_attempts: int = 0,                      # минимум попыток (иначе 1 успешный = 100%)
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Экспорт каналов с фильтрами.

    Query params:
      source:
        parsed         — ParsedChannel (только сохранённые каналы юзера)
        tgstat_checked — TgstatCheckedChannel (ВСЁ проверенное web-скрейпером
                         на TGStat: тут 1000+ каналов даже с комментами,
                         которые не попали в ParsedChannel из-за UPSERT).
      folder                — имя папки (только для source=parsed).
                              None = все, пустая строка = только без папки.
      only_with_comments    — оставить только has_comments=True
      min_subscribers       — оставить только subs >= N
      max_subscribers       — оставить только subs <= N
      format:
        csv        — полный CSV с колонками
        txt        — по одному @username на строку (для copy-paste)
        txt_links  — по одной t.me/xxx-ссылке на строку

    Content-Disposition имя файла собирается из фильтров: чтобы юзер сразу
    видел что скачал: channels_folder-crypto_with-comments.csv
    """
    import urllib.parse as _up

    # ── Собираем rows (username, title, subs, has_comments, folder, last_post, query) ──
    rows = []
    if source == "tgstat_checked":
        from models.tgstat_checked_channel import TgstatCheckedChannel
        q = select(TgstatCheckedChannel).where(TgstatCheckedChannel.user_id == current_user.id)
        if only_with_comments:
            q = q.where(TgstatCheckedChannel.has_comments == True)
        if min_subscribers:
            q = q.where(TgstatCheckedChannel.subscribers >= min_subscribers)
        if max_subscribers:
            q = q.where(TgstatCheckedChannel.subscribers <= max_subscribers)
        q = q.order_by(TgstatCheckedChannel.subscribers.desc().nullslast())
        for c in (await db.execute(q)).scalars().all():
            rows.append({
                "username": c.username or "",
                "title": c.title or "",
                "subscribers": c.subscribers or 0,
                "has_comments": bool(c.has_comments),
                "folder": "",
                "last_post": c.checked_at.isoformat() if c.checked_at else "",
                "query": f"tgstat_web:{c.last_job_id or ''}",
            })
    else:
        # source=parsed (default) — из ParsedChannel
        q = select(ParsedChannel).where(ParsedChannel.user_id == current_user.id)
        if folder is not None:
            q = q.where(ParsedChannel.folder == folder)
        if only_with_comments:
            q = q.where(ParsedChannel.has_comments == True)
        if min_subscribers:
            q = q.where(ParsedChannel.subscribers >= min_subscribers)
        if max_subscribers:
            q = q.where(ParsedChannel.subscribers <= max_subscribers)
        q = q.order_by(ParsedChannel.subscribers.desc())
        for c in (await db.execute(q)).scalars().all():
            rows.append({
                "username": c.username or "",
                "title": c.title or "",
                "subscribers": c.subscribers or 0,
                "has_comments": bool(c.has_comments),
                "folder": c.folder or "",
                "last_post": c.last_post_date.isoformat() if c.last_post_date else "",
                "query": c.search_query or "",
            })

    # ── Фильтр по проходимости (ChannelBanStats) ──
    if min_pass_rate > 0 or no_skips or min_attempts > 0:
        from models.channel_ban_stats import ChannelBanStats
        _usernames = [r["username"] for r in rows if r["username"]]
        _stats_q = await db.execute(select(ChannelBanStats).where(
            ChannelBanStats.user_id == current_user.id,
            ChannelBanStats.channel_username.in_(_usernames),
        ))
        _stats_by_username = {s.channel_username: s for s in _stats_q.scalars().all()}
        _filtered = []
        for r in rows:
            st = _stats_by_username.get(r["username"])
            if not st:
                # Нет статистики → не знаем прошёл или нет. Если запросили фильтр
                # min_attempts>0 или min_pass_rate>0 — считаем что не подходит.
                if min_attempts > 0 or min_pass_rate > 0:
                    continue
                # no_skips без min_attempts — пропускаем каналы без стат (безопасно)
                _filtered.append(r)
                continue
            if min_attempts > 0 and (st.total_attempts or 0) < min_attempts:
                continue
            if no_skips and (st.skipped_count or 0) > 0:
                continue
            if min_pass_rate > 0 and st.pass_rate < min_pass_rate:
                continue
            # добавляем метрики в row (пригодится для CSV)
            r["ban_stats_total"] = st.total_attempts or 0
            r["ban_stats_banned"] = st.banned_count or 0
            r["ban_stats_skipped"] = st.skipped_count or 0
            r["ban_stats_pass_rate"] = st.pass_rate
            _filtered.append(r)
        rows = _filtered

    # ── Имя файла с фильтрами ──
    fname_parts = ["channels"]
    if source == "tgstat_checked":
        fname_parts.append("tgstat")
    if folder is not None and source == "parsed":
        fname_parts.append("folder-" + (folder or "none"))
    if only_with_comments:
        fname_parts.append("with-comments")
    if min_subscribers:
        fname_parts.append(f"min{min_subscribers}")
    if min_pass_rate > 0:
        fname_parts.append(f"pass{int(min_pass_rate)}pct")
    if no_skips:
        fname_parts.append("no-skips")
    ext = {"csv": "csv", "txt": "txt", "txt_links": "txt"}.get(format, "csv")
    filename = "_".join(fname_parts) + f".{ext}"
    ascii_name = filename.encode("ascii", "ignore").decode() or "channels.csv"
    utf8_encoded = _up.quote(filename, safe="")

    output = io.StringIO()
    if format == "txt":
        # @username по одному на строку — готово к вставке в commenting кампанию.
        # Санитайзим на всякий случай — оставляем только [a-zA-Z0-9_] в username.
        import re as _re_uname
        for r in rows:
            u = (r["username"] or "").strip().lstrip("@")
            u = _re_uname.sub(r"[^a-zA-Z0-9_]", "", u)
            if u:
                output.write("@" + u + "\n")
        media = "text/plain; charset=utf-8"
    elif format == "txt_links":
        for r in rows:
            if r["username"]:
                output.write(f"https://t.me/{r['username']}\n")
        media = "text/plain; charset=utf-8"
    else:
        writer = csv.writer(output)
        writer.writerow(["username", "title", "subscribers", "has_comments", "folder", "last_post", "query"])
        for r in rows:
            writer.writerow([
                f"@{r['username']}", r["title"], r["subscribers"], r["has_comments"],
                r["folder"], r["last_post"], r["query"],
            ])
        media = "text/csv; charset=utf-8"

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type=media,
        headers={
            "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{utf8_encoded}",
            "X-Records-Count": str(len(rows)),
        },
    )


@router.get("/export/counts")
async def export_counts(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Счётчики для UI-модалки экспорта: сколько каналов в каждом источнике,
    сколько из них с комментами. Чтобы юзер сразу видел «Parsed: 555 / 1234,
    TGStat checked: 1277 / 5000» и понимал что качает."""
    from models.tgstat_checked_channel import TgstatCheckedChannel

    # ParsedChannel
    parsed_total_q = await db.execute(
        select(func.count(ParsedChannel.id))
        .where(ParsedChannel.user_id == current_user.id)
    )
    parsed_wc_q = await db.execute(
        select(func.count(ParsedChannel.id))
        .where(ParsedChannel.user_id == current_user.id,
               ParsedChannel.has_comments == True)
    )
    # TgstatChecked
    tg_total_q = await db.execute(
        select(func.count(TgstatCheckedChannel.id))
        .where(TgstatCheckedChannel.user_id == current_user.id)
    )
    tg_wc_q = await db.execute(
        select(func.count(TgstatCheckedChannel.id))
        .where(TgstatCheckedChannel.user_id == current_user.id,
               TgstatCheckedChannel.has_comments == True)
    )
    return {
        "parsed": {
            "total": parsed_total_q.scalar() or 0,
            "with_comments": parsed_wc_q.scalar() or 0,
        },
        "tgstat_checked": {
            "total": tg_total_q.scalar() or 0,
            "with_comments": tg_wc_q.scalar() or 0,
        },
    }


@router.post("/import")
async def import_channels(
    body: ImportRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    added = 0
    for raw in body.channels:
        username = raw.strip().replace("@", "").replace("https://t.me/", "")
        if not username:
            continue
        existing = await db.execute(
            select(ParsedChannel).where(ParsedChannel.user_id == current_user.id, ParsedChannel.username == username)
        )
        if existing.scalar_one_or_none():
            continue
        db.add(ParsedChannel(user_id=current_user.id, username=username, title=username, search_query="import"))
        added += 1

    await db.flush()
    return {"added": added}


# ── Папки каналов ────────────────────────────────────────────

@router.get("/folders")
async def list_folders(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Тепер дістаємо не тільки папку, але й статус коментарів
    result = await db.execute(
        select(ParsedChannel.folder, ParsedChannel.has_comments).where(
            ParsedChannel.user_id == current_user.id,
            ParsedChannel.folder != "",
            ParsedChannel.folder.isnot(None)
        )
    )
    rows = result.all()
    folder_counts = {}
    
    for folder, has_comments in rows:
        if folder not in folder_counts:
            folder_counts[folder] = {"count": 0, "with_comments": 0}
        folder_counts[folder]["count"] += 1
        if has_comments:
            folder_counts[folder]["with_comments"] += 1

    return [
        {"name": k, "count": v["count"], "with_comments": v["with_comments"]} 
        for k, v in sorted(folder_counts.items())
    ]


@router.get("/folders/{folder_name}/channels")
async def get_folder_channels(
    folder_name: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from models.channel_ban_stats import ChannelBanStats

    result = await db.execute(
        select(ParsedChannel).where(
            ParsedChannel.user_id == current_user.id,
            ParsedChannel.folder == folder_name,
        )
    )
    channels = result.scalars().all()

    usernames = [c.username for c in channels if c.username]
    stats_map = {}
    if usernames:
        stats_r = await db.execute(
            select(ChannelBanStats).where(
                ChannelBanStats.user_id == current_user.id,
                ChannelBanStats.channel_username.in_(usernames),
            )
        )
        for s in stats_r.scalars().all():
            _sk = s.skipped_count or 0
            _bn = s.banned_count or 0
            pass_rate = 100.0 if s.total_attempts == 0 else round(
                max(0, s.total_attempts - _bn - _sk) / s.total_attempts * 100, 1
            )
            stats_map[s.channel_username] = pass_rate

    return [{
        "id": c.id, "username": c.username, "title": c.title,
        "subscribers": c.subscribers, "has_comments": c.has_comments,
        "pass_rate": stats_map.get(c.username),
        "folder": c.folder,
    } for c in channels]

class SetFolderRequest(BaseModel):
    channel_ids: list[int]
    folder: str


@router.post("/set-folder")
async def set_folder(
    body: SetFolderRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ParsedChannel).where(
            ParsedChannel.user_id == current_user.id,
            ParsedChannel.id.in_(body.channel_ids),
        )
    )
    channels = result.scalars().all()
    for ch in channels:
        ch.folder = body.folder
    await db.flush()
    return {"updated": len(channels), "folder": body.folder}


@router.patch("/channels/{channel_id}/folder")
async def update_channel_folder(
    channel_id: int,
    body: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ParsedChannel).where(ParsedChannel.id == channel_id, ParsedChannel.user_id == current_user.id)
    )
    ch = result.scalar_one_or_none()
    if not ch:
        raise HTTPException(status_code=404, detail="Канал не найден")
    ch.folder = body.get("folder", "")
    await db.flush()
    return {"id": ch.id, "folder": ch.folder}
@router.get("/search/progress")
async def get_search_progress(
    current_user: User = Depends(get_current_user),
):
    """Статус текущего парсинга"""
    import redis as redis_lib
    import os
    r = redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    progress = r.get(f"parser:progress:{current_user.id}")
    if not progress:
        return {"status": "idle"}

    try:
        parts = progress.decode().split("|")
        return {
            "status": parts[0],        # running | done | error
            "found": int(parts[1]),
            "saved": int(parts[2]),
            "total_keywords": int(parts[3]),
            "current": parts[4] if len(parts) > 4 else "",
        }
    except Exception:
        return {"status": "idle"}


@router.post("/search/stop")
async def stop_search(
    current_user: User = Depends(get_current_user),
):
    """Прерывает текущий парсинг"""
    import redis as redis_lib
    r = redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    r.setex(f"parser:stop:{current_user.id}", 3600, "1")
    task_id = r.get(f"parser:task_id:{current_user.id}")
    if task_id:
        try:
            from celery_app import celery_app
            celery_app.control.revoke(task_id.decode(), terminate=True, signal="SIGTERM")
        except Exception:
            pass
        r.delete(f"parser:task_id:{current_user.id}")
    return {"status": "stop requested"}


# ══════════════════════════════════════════════════════════
# Multi-account поиск
# ══════════════════════════════════════════════════════════

class SearchMultiRequest(BaseModel):
    account_ids: list[int]                                  # какие акки использовать
    keywords: str
    min_subscribers: int = 0
    max_subscribers: int = 10_000_000
    only_with_comments: bool = True                         # precise_check ВСЕГДА, флаг только "фильтровать?"
    active_hours: int = 0
    name_endings: Optional[str] = None
    name_contains: Optional[str] = None
    limit_per_keyword: int = 50
    max_channels_per_acc: int = 500
    # Диапазон случайной параллельности: сколько прокси одновременно
    # (реальное значение = random.randint(min, min(max, N_proxies))).
    min_concurrency: int = 2
    max_concurrency: int = 7
    pause_between_keywords_min: float = 3.0
    pause_between_keywords_max: float = 6.0
    pause_between_channels_min: float = 0.8
    pause_between_channels_max: float = 1.5


def _multi_check_no_duplicate(r, user_id: int):
    """Guard: если у юзера уже идёт multi-парсер (progress running, heartbeat
    свежий) — 409. Иначе получим два параллельных таска, они пишут в разные
    ключи, но грузят одни и те же акки. Юзер должен либо дождаться, либо
    остановить."""
    import json as _json, time as _t
    # Сканим все multi-progress ключи юзера
    for key in r.scan_iter(match=f"parser:multi:progress:{user_id}:*"):
        raw = r.get(key)
        if not raw:
            continue
        try:
            d = _json.loads(raw)
        except Exception:
            continue
        if d.get("status") != "running":
            continue
        ts = int(d.get("ts") or 0)
        if _t.time() - ts > 120:
            continue    # stale, considered dead
        active_job = d.get("job_id") or ""
        found_total = d.get("found_total", 0)
        acc_total = d.get("total_accounts", 0)
        raise HTTPException(
            status_code=409,
            detail=(
                f"Уже идёт multi-парсер (job={active_job[:12]}): найдено {found_total}, "
                f"акков {acc_total}. Дождись окончания или нажми Стоп."
            ),
        )


@router.post("/search/multi")
async def search_multi(
    body: SearchMultiRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Мульти-акк парсер каналов.

    Отличия от /search:
      * accepts N account_ids
      * группирует по прокси, N воркеров параллельно (random 2..7)
      * FloodWait → аккаунт уходит на cooldown ≥ 1час, без retry
      * precise has_comments вшит (не нужно потом отдельно верифить)
    """
    import redis as redis_lib
    r = redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    _multi_check_no_duplicate(r, current_user.id)

    keywords = [k.strip() for k in body.keywords.split(",") if k.strip()]
    if not keywords:
        raise HTTPException(status_code=400, detail="Укажите хотя бы одно ключевое слово")
    if not body.account_ids:
        raise HTTPException(status_code=400, detail="Не выбран ни один аккаунт")

    # проверка — что акки принадлежат юзеру
    acc_r = await db.execute(
        select(TelegramAccount.id).where(
            TelegramAccount.id.in_(body.account_ids),
            TelegramAccount.user_id == current_user.id,
        )
    )
    owned = {r[0] for r in acc_r.all()}
    missing = [x for x in body.account_ids if x not in owned]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Не твои акки: {missing[:5]}{'...' if len(missing)>5 else ''}",
        )

    import uuid as _uuid, time as _t
    job_id = f"multi_{int(_t.time())}_{_uuid.uuid4().hex[:6]}"

    from celery_app import celery_app
    params = body.model_dump()
    params["job_id"] = job_id
    task = celery_app.send_task(
        "tasks.parser_multi_tasks.run_parser_multi_search",
        args=[current_user.id, list(owned), params],
        queue="parsers",
    )
    return {
        "task_id": task.id,
        "job_id": job_id,
        "status": "started",
        "accounts_scheduled": len(owned),
        "keywords": len(keywords),
        "message": f"Multi-парсинг: {len(owned)} акк, {len(keywords)} ключей",
    }


@router.get("/search/multi/progress")
async def get_search_multi_progress(
    job_id: str = Query(...),
    current_user: User = Depends(get_current_user),
):
    """Snapshot per-account прогресса multi-парсинга.
    Формат в docstring tasks.parser_multi_tasks.

    Auto-stale: если status=running но heartbeat старше 120с — считаем
    воркер мёртвым, отдаём status=error чтобы UI мгновенно показал.
    """
    import redis as redis_lib
    import json as _json, time as _t
    r = redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    prog_key = f"parser:multi:progress:{current_user.id}:{job_id}"
    raw = r.get(prog_key)
    if not raw:
        return {"status": "idle"}
    try:
        d = _json.loads(raw)
    except Exception:
        return {"status": "idle"}
    # Stale detection
    if d.get("status") == "running":
        ts = int(d.get("ts") or 0)
        age = int(_t.time()) - ts
        if age > 120:
            d["status"] = "error"
            d["stale"] = True
            d["error"] = (
                f"Воркер не отвечает {age}с — вероятно упал (рестарт celery, "
                f"crash или OOM). Найдено {d.get('found_total',0)} каналов. "
                f"Запусти заново — акки на cooldown будут пропущены."
            )
            try:
                r.setex(prog_key, 3600, _json.dumps(d, ensure_ascii=False))
            except Exception:
                pass
    return d


@router.get("/search/multi/active")
async def get_active_multi(
    current_user: User = Depends(get_current_user),
):
    """Список активных multi-парсер задач юзера. UI подхватывает на mount
    чтобы бейдж «Multi-парсер идёт» показывался даже после F5.
    Возвращает только те где status=running + heartbeat < 120с."""
    import redis as redis_lib
    import json as _json, time as _t
    r = redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    out = []
    now_ts = int(_t.time())
    for key in r.scan_iter(match=f"parser:multi:progress:{current_user.id}:*"):
        raw = r.get(key)
        if not raw:
            continue
        try:
            d = _json.loads(raw)
        except Exception:
            continue
        ts = int(d.get("ts") or 0)
        age = now_ts - ts
        status = d.get("status")
        # Показываем running (свежие) И recently-done/cancelled (< 5 мин)
        # чтобы юзер после F5 увидел итоги последнего run'а.
        if status == "running" and age > 120:
            continue    # stale, будет marked как error при следующем /progress
        if status in ("done", "cancelled", "error") and age > 300:
            continue    # старые — не тянем
        out.append({
            "job_id": d.get("job_id"),
            "status": status,
            "found_total": d.get("found_total", 0),
            "saved_total": d.get("saved_total", 0),
            "total_accounts": d.get("total_accounts", 0),
            "concurrency": d.get("concurrency", 0),
            "started_at": d.get("started_at"),
            "age_sec": age,
        })
    # Самые свежие сверху
    out.sort(key=lambda x: x.get("started_at") or 0, reverse=True)
    return {"jobs": out}


@router.post("/search/multi/stop")
async def stop_search_multi(
    job_id: str = Query(...),
    current_user: User = Depends(get_current_user),
):
    """Ставит cancel-флаг. Воркеры увидят на ближайшем check и выйдут
    (мягкая остановка — акки текущей итерации доделываются)."""
    import redis as redis_lib
    r = redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    r.setex(f"parser:multi:stop:{current_user.id}:{job_id}", 3600, "1")
    return {"status": "stopping", "job_id": job_id}


@router.post("/search/multi/reset")
async def reset_search_multi(
    job_id: str = Query(...),
    current_user: User = Depends(get_current_user),
):
    """Force-reset зависшего multi-парсера.

    Что делает:
      1. celery_app.control.revoke(task_id, terminate=True, signal=SIGTERM)
         → пришибает celery-воркер даже если он застрял в blocking Telethon.
      2. Ставит cancel-флаг (на случай если revoke не сработает).
      3. Помечает snapshot в Redis как status=error чтобы UI перестал крутить.
      4. Удаляет task_id-ключ.
    """
    import redis as redis_lib
    import json as _json, time as _t
    r = redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    revoked = False
    tk_key = f"parser:multi:task_id:{current_user.id}:{job_id}"
    task_id = r.get(tk_key)
    if task_id:
        try:
            from celery_app import celery_app
            tid = task_id.decode() if isinstance(task_id, bytes) else str(task_id)
            celery_app.control.revoke(tid, terminate=True, signal="SIGTERM")
            revoked = True
        except Exception as e:
            logger.warning(f"[multi_reset] revoke failed: {e}")
    # Cancel-флаг — как второй пояс безопасности
    r.setex(f"parser:multi:stop:{current_user.id}:{job_id}", 3600, "1")
    # Помечаем snapshot как error
    prog_key = f"parser:multi:progress:{current_user.id}:{job_id}"
    raw = r.get(prog_key)
    if raw:
        try:
            d = _json.loads(raw)
            d["status"] = "error"
            d["stale"] = True
            d["error"] = f"Force-reset юзером{' (revoked)' if revoked else ''}"
            d["ts"] = int(_t.time())
            r.setex(prog_key, 3600, _json.dumps(d, ensure_ascii=False))
        except Exception:
            pass
    # Убираем task_id — новый запуск сгенерит свой
    r.delete(tk_key)
    return {"status": "reset", "job_id": job_id, "revoked": revoked}


@router.get("/search/multi/cooldowns")
async def get_acc_cooldowns(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Список аккаунтов юзера с активным parser:acc_cooldown флагом.
    UI показывает: «эти акки на перекуре, будут пропущены»."""
    import redis as redis_lib
    r = redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    accs_r = await db.execute(
        select(TelegramAccount.id, TelegramAccount.phone, TelegramAccount.first_name)
        .where(TelegramAccount.user_id == current_user.id)
    )
    out = []
    now_ts = int(__import__("time").time())
    for acc_id, phone, first_name in accs_r.all():
        cd = r.get(f"parser:acc_cooldown:{acc_id}")
        if not cd:
            continue
        try:
            expires = int(cd)
        except Exception:
            expires = now_ts
        out.append({
            "account_id": acc_id,
            "phone": phone,
            "first_name": first_name,
            "expires_at": expires,
            "seconds_left": max(0, expires - now_ts),
        })
    return out


@router.post("/search/multi/cooldowns/clear")
async def clear_acc_cooldown(
    account_id: int = Query(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Ручной сброс cooldown для одного аккаунта (юзер знает лучше).
    Проверка что акк принадлежит юзеру."""
    r_ = await db.execute(
        select(TelegramAccount.id).where(
            TelegramAccount.id == account_id,
            TelegramAccount.user_id == current_user.id,
        )
    )
    if not r_.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Аккаунт не найден")
    import redis as redis_lib
    r = redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    r.delete(f"parser:acc_cooldown:{account_id}")
    return {"status": "cleared", "account_id": account_id}
@router.get("/whitelist")
async def get_whitelist(
    min_rate: float = 0,  # фильтр по минимальной проходимости
    sort_by: str = "pass_rate",  # pass_rate | attempts | recent
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Статистика проходимости каналов"""
    from models.channel_ban_stats import ChannelBanStats
    from sqlalchemy import func

    result = await db.execute(
        select(ChannelBanStats).where(ChannelBanStats.user_id == current_user.id)
    )
    stats = result.scalars().all()

    out = []
    for s in stats:
        _sk = s.skipped_count or 0
        _bn = s.banned_count or 0
        pass_rate = 100.0 if s.total_attempts == 0 else round(
            max(0, s.total_attempts - _bn - _sk) / s.total_attempts * 100, 1
        )
        if pass_rate < min_rate:
            continue
        out.append({
            "id": s.id,
            "channel_username": s.channel_username,
            "total_attempts": s.total_attempts,
            "banned_count": _bn,
            "skipped_count": _sk,
            "pass_rate": pass_rate,
            "last_ban_reason": s.last_ban_reason,
            "last_skip_reason": s.last_skip_reason or "",
            "last_updated": s.last_updated.isoformat() + "Z",
        })

    # Сортировка
    if sort_by == "attempts":
        out.sort(key=lambda x: x["total_attempts"], reverse=True)
    elif sort_by == "recent":
        out.sort(key=lambda x: x["last_updated"], reverse=True)
    else:
        out.sort(key=lambda x: x["pass_rate"], reverse=True)

    return out


@router.delete("/whitelist/{stat_id}", status_code=204)
async def delete_whitelist_entry(
    stat_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Удалить запись (сбросить статистику канала)"""
    from models.channel_ban_stats import ChannelBanStats
    result = await db.execute(
        select(ChannelBanStats).where(
            ChannelBanStats.id == stat_id,
            ChannelBanStats.user_id == current_user.id,
        )
    )
    s = result.scalar_one_or_none()
    if s:
        await db.delete(s)
        await db.flush()


@router.delete("/whitelist")
async def wipe_whitelist(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Полностью очистить всю проходимость юзера — все ChannelBanStats
    удаляются. Юзкейс: изначальные записи созданы во время неполного
    цикла (без верификации удаления), pass_rate там нерелевантный.
    После очистки — начинаем считать с нуля через новый verify-механизм.
    """
    from models.channel_ban_stats import ChannelBanStats
    from sqlalchemy import delete as sa_delete
    result = await db.execute(
        sa_delete(ChannelBanStats).where(ChannelBanStats.user_id == current_user.id)
    )
    await db.flush()
    return {"deleted": result.rowcount or 0}

# ══════════════════════════════════════════════════════════
# Similar channels crawler (обход графа похожих каналов)
# ══════════════════════════════════════════════════════════

class SimilarCrawlRequest(BaseModel):
    account_id: int
    seeds: list[str]                   # username каналов (без @)
    max_depth: int = 2                 # 1 = только 1 уровень, 2 = с похожих тоже идём дальше, 3 = ещё глубже
    max_channels: int = 1000
    folder: str = ""
    pause_min: float = 2.0
    pause_max: float = 5.0


@router.post("/similar/start")
async def start_similar_crawl(
    body: SimilarCrawlRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Запускает обход графа похожих каналов в фоне."""
    if not body.seeds:
        raise HTTPException(status_code=400, detail="Укажи хотя бы один seed-канал")

    # Проверка аккаунта
    acc_r = await db.execute(
        select(TelegramAccount).where(
            TelegramAccount.id == body.account_id,
            TelegramAccount.user_id == current_user.id,
        )
    )
    acc = acc_r.scalar_one_or_none()
    if not acc:
        raise HTTPException(status_code=404, detail="Аккаунт не найден")
    if not acc.proxy_id:
        raise HTTPException(status_code=400, detail="У аккаунта нет прокси")

    from celery_app import celery_app
    task = celery_app.send_task(
        "tasks.parser_similar_tasks.run_similar_crawler",
        args=[current_user.id, body.account_id, body.model_dump()],
        queue="ai_dialogs",
    )

    return {
        "task_id": task.id,
        "status": "started",
        "message": f"Crawler запущен: {len(body.seeds)} seeds, глубина {body.max_depth}",
    }


@router.get("/similar/progress")
async def get_similar_progress(
    current_user: User = Depends(get_current_user),
):
    """Прогресс обхода похожих."""
    import redis as redis_lib
    import os
    r = redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    progress = r.get(f"parser:similar:progress:{current_user.id}")
    if not progress:
        return {"status": "idle"}

    try:
        parts = progress.decode().split("|")
        return {
            "status": parts[0],        # running | done | error
            "found": int(parts[1]),
            "saved": int(parts[2]),
            "queue": int(parts[3]),    # размер очереди
            "current": parts[4] if len(parts) > 4 else "",
        }
    except Exception:
        return {"status": "idle"}


@router.post("/similar/stop")
async def stop_similar_crawl(
    current_user: User = Depends(get_current_user),
):
    import redis as redis_lib
    r = redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    r.setex(f"parser:similar:stop:{current_user.id}", 3600, "1")
    task_id = r.get(f"parser:similar:task_id:{current_user.id}")
    if task_id:
        try:
            from celery_app import celery_app
            celery_app.control.revoke(task_id.decode(), terminate=True, signal="SIGTERM")
        except Exception:
            pass
        r.delete(f"parser:similar:task_id:{current_user.id}")
    return {"status": "stopping"}


# ══════════════════════════════════════════════════════════
# Similar channels crawler (обход графа похожих каналов)
# ══════════════════════════════════════════════════════════

class SimilarCrawlRequest(BaseModel):
    account_id: int
    seeds: list[str]                   # username каналов (без @)
    max_depth: int = 2                 # 1 = только 1 уровень, 2 = с похожих тоже идём дальше, 3 = ещё глубже
    max_channels: int = 1000
    folder: str = ""
    pause_min: float = 2.0
    pause_max: float = 5.0


@router.post("/similar/start")
async def start_similar_crawl(
    body: SimilarCrawlRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Запускает обход графа похожих каналов в фоне."""
    if not body.seeds:
        raise HTTPException(status_code=400, detail="Укажи хотя бы один seed-канал")

    # Проверка аккаунта
    acc_r = await db.execute(
        select(TelegramAccount).where(
            TelegramAccount.id == body.account_id,
            TelegramAccount.user_id == current_user.id,
        )
    )
    acc = acc_r.scalar_one_or_none()
    if not acc:
        raise HTTPException(status_code=404, detail="Аккаунт не найден")
    if not acc.proxy_id:
        raise HTTPException(status_code=400, detail="У аккаунта нет прокси")

    from celery_app import celery_app
    task = celery_app.send_task(
        "tasks.parser_similar_tasks.run_similar_crawler",
        args=[current_user.id, body.account_id, body.model_dump()],
        queue="ai_dialogs",
    )

    return {
        "task_id": task.id,
        "status": "started",
        "message": f"Crawler запущен: {len(body.seeds)} seeds, глубина {body.max_depth}",
    }


@router.get("/similar/progress")
async def get_similar_progress(
    current_user: User = Depends(get_current_user),
):
    """Прогресс обхода похожих."""
    import redis as redis_lib
    import os
    r = redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    progress = r.get(f"parser:similar:progress:{current_user.id}")
    if not progress:
        return {"status": "idle"}

    try:
        parts = progress.decode().split("|")
        return {
            "status": parts[0],        # running | done | error
            "found": int(parts[1]),
            "saved": int(parts[2]),
            "queue": int(parts[3]),    # размер очереди
            "current": parts[4] if len(parts) > 4 else "",
        }
    except Exception:
        return {"status": "idle"}


# ══════════════════════════════════════════════════════════
# Multi-account паутинка (crawler)
# ══════════════════════════════════════════════════════════

class SimilarMultiRequest(BaseModel):
    account_ids: list[int]
    seeds: list[str]
    max_depth: int = 2
    max_channels: int = 1000
    folder: str = ""
    pause_min: float = 8.0
    pause_max: float = 15.0
    min_concurrency: int = 2
    max_concurrency: int = 7
    # Оставлять только каналы с открытыми комментариями (linked discussion).
    # Precise-check вшит: GetFullChannel во время crawl'а. +1 запрос TG
    # per candidate, но сохраняем сразу правильный флаг → не нужно потом
    # отдельно верифить.
    only_with_comments: bool = True


def _similar_multi_check_no_duplicate(r, user_id: int):
    """Guard: если уже идёт multi-паутинка — 409."""
    import json as _json, time as _t
    for key in r.scan_iter(match=f"parser:similar_multi:progress:{user_id}:*"):
        raw = r.get(key)
        if not raw:
            continue
        try: d = _json.loads(raw)
        except Exception: continue
        if d.get("status") != "running":
            continue
        ts = int(d.get("ts") or 0)
        if _t.time() - ts > 120:
            continue
        active_job = d.get("job_id") or ""
        raise HTTPException(
            status_code=409,
            detail=(
                f"Уже идёт multi-паутинка (job={active_job[:12]}): найдено "
                f"{d.get('found_total',0)}. Дождись или нажми Стоп."
            ),
        )


@router.post("/similar/multi")
async def start_similar_multi(
    body: SimilarMultiRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Мульти-акк паутинка. Отличия от /similar/start:
      * accepts N account_ids
      * random concurrency 2..7 по прокси
      * FloodWait → cooldown ≥1час, без retry
      * cooperative yield с warmup/commenting/refix через account_lock
      * split seeds round-robin среди активных акков, общий Redis SET
        для кросс-акк дедупа
    """
    import redis as redis_lib
    r = redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    _similar_multi_check_no_duplicate(r, current_user.id)

    if not body.seeds:
        raise HTTPException(status_code=400, detail="Укажи хотя бы один seed-канал")
    if not body.account_ids:
        raise HTTPException(status_code=400, detail="Не выбран ни один аккаунт")

    acc_r = await db.execute(
        select(TelegramAccount.id).where(
            TelegramAccount.id.in_(body.account_ids),
            TelegramAccount.user_id == current_user.id,
        )
    )
    owned = {r_[0] for r_ in acc_r.all()}
    missing = [x for x in body.account_ids if x not in owned]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Не твои акки: {missing[:5]}{'...' if len(missing)>5 else ''}",
        )

    import uuid as _uuid, time as _t
    job_id = f"similar_multi_{int(_t.time())}_{_uuid.uuid4().hex[:6]}"

    from celery_app import celery_app
    params = body.model_dump()
    params["job_id"] = job_id
    task = celery_app.send_task(
        "tasks.parser_similar_multi_tasks.run_similar_multi_crawler",
        args=[current_user.id, list(owned), params],
        queue="parsers",
    )
    return {
        "task_id": task.id,
        "job_id": job_id,
        "status": "started",
        "accounts_scheduled": len(owned),
        "seeds": len(body.seeds),
        "message": f"Multi-паутинка: {len(owned)} акк, {len(body.seeds)} seeds",
    }


@router.get("/similar/multi/progress")
async def get_similar_multi_progress(
    job_id: str = Query(...),
    current_user: User = Depends(get_current_user),
):
    """Snapshot per-acc прогресса паутинки. Auto-stale если heartbeat >120с."""
    import redis as redis_lib
    import json as _json, time as _t
    r = redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    prog_key = f"parser:similar_multi:progress:{current_user.id}:{job_id}"
    raw = r.get(prog_key)
    if not raw:
        return {"status": "idle"}
    try:
        d = _json.loads(raw)
    except Exception:
        return {"status": "idle"}
    if d.get("status") == "running":
        ts = int(d.get("ts") or 0)
        age = int(_t.time()) - ts
        if age > 120:
            d["status"] = "error"
            d["stale"] = True
            d["error"] = (
                f"Воркер не отвечает {age}с — вероятно упал. "
                f"Найдено {d.get('found_total',0)} каналов. Запусти заново."
            )
            try:
                r.setex(prog_key, 3600, _json.dumps(d, ensure_ascii=False))
            except Exception:
                pass
    return d


@router.get("/similar/multi/active")
async def get_active_similar_multi(
    current_user: User = Depends(get_current_user),
):
    """Список активных multi-паутинок юзера (для persistence после F5)."""
    import redis as redis_lib
    import json as _json, time as _t
    r = redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    out = []
    now_ts = int(_t.time())
    for key in r.scan_iter(match=f"parser:similar_multi:progress:{current_user.id}:*"):
        raw = r.get(key)
        if not raw:
            continue
        try: d = _json.loads(raw)
        except Exception: continue
        ts = int(d.get("ts") or 0)
        age = now_ts - ts
        status = d.get("status")
        if status == "running" and age > 120:
            continue
        if status in ("done", "cancelled", "error") and age > 300:
            continue
        out.append({
            "job_id": d.get("job_id"),
            "status": status,
            "found_total": d.get("found_total", 0),
            "saved_total": d.get("saved_total", 0),
            "total_accounts": d.get("total_accounts", 0),
            "concurrency": d.get("concurrency", 0),
            "started_at": d.get("started_at"),
            "age_sec": age,
        })
    out.sort(key=lambda x: x.get("started_at") or 0, reverse=True)
    return {"jobs": out}


@router.post("/similar/multi/stop")
async def stop_similar_multi(
    job_id: str = Query(...),
    current_user: User = Depends(get_current_user),
):
    """Мягкий стоп: cancel-флаг → воркеры доделывают текущий канал и выходят."""
    import redis as redis_lib
    r = redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    r.setex(f"parser:similar_multi:stop:{current_user.id}:{job_id}", 3600, "1")
    return {"status": "stopping", "job_id": job_id}


@router.post("/similar/multi/reset")
async def reset_similar_multi(
    job_id: str = Query(...),
    current_user: User = Depends(get_current_user),
):
    """Force-reset: revoke celery + cancel + mark error."""
    import redis as redis_lib
    import json as _json, time as _t
    r = redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    revoked = False
    tk_key = f"parser:similar_multi:task_id:{current_user.id}:{job_id}"
    task_id = r.get(tk_key)
    if task_id:
        try:
            from celery_app import celery_app
            tid = task_id.decode() if isinstance(task_id, bytes) else str(task_id)
            celery_app.control.revoke(tid, terminate=True, signal="SIGTERM")
            revoked = True
        except Exception as e:
            logger.warning(f"[similar_multi_reset] revoke fail: {e}")
    r.setex(f"parser:similar_multi:stop:{current_user.id}:{job_id}", 3600, "1")
    prog_key = f"parser:similar_multi:progress:{current_user.id}:{job_id}"
    raw = r.get(prog_key)
    if raw:
        try:
            d = _json.loads(raw)
            d["status"] = "error"
            d["stale"] = True
            d["error"] = f"Force-reset юзером{' (revoked)' if revoked else ''}"
            d["ts"] = int(_t.time())
            r.setex(prog_key, 3600, _json.dumps(d, ensure_ascii=False))
        except Exception:
            pass
    r.delete(tk_key)
    return {"status": "reset", "job_id": job_id, "revoked": revoked}


# ══════════════════════════════════════════════════════════
# Comments Verifier — проверка has_comments пачками
# ══════════════════════════════════════════════════════════

class VerifyCommentsRequest(BaseModel):
    account_id: int
    folder: str = ""
    limit: int = 200
    pause_min: float = 2.0
    pause_max: float = 4.0
    only_unverified: bool = True
    active_hours: int = 0
    min_verify_interval_days: int = 0
    stop_on_flood: bool = True            # на первом FloodWait — стоп
    flood_cooldown_sec: int = 300         # если stop_on_flood=False: сколько ждать сверх FloodWait перед retry


@router.post("/verify/start")
async def start_verify_comments(
    body: VerifyCommentsRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Запускает проверку реальных has_comments через GetFullChannel."""
    acc_r = await db.execute(
        select(TelegramAccount).where(
            TelegramAccount.id == body.account_id,
            TelegramAccount.user_id == current_user.id,
        )
    )
    acc = acc_r.scalar_one_or_none()
    if not acc:
        raise HTTPException(status_code=404, detail="Аккаунт не найден")
    if not acc.proxy_id:
        raise HTTPException(status_code=400, detail="У аккаунта нет прокси")

    from celery_app import celery_app
    task = celery_app.send_task(
        "tasks.parser_similar_tasks.run_verify_comments",
        args=[current_user.id, body.account_id, body.model_dump()],
        queue="ai_dialogs",
    )

    return {
        "task_id": task.id,
        "status": "started",
        "message": f"Проверка запущена (лимит {body.limit})",
    }


@router.get("/verify/progress")
async def get_verify_progress(
    current_user: User = Depends(get_current_user),
):
    """Прогресс проверки комментов."""
    import redis as redis_lib
    import os
    r = redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    progress = r.get(f"parser:verify:progress:{current_user.id}")
    if not progress:
        return {"status": "idle"}

    try:
        parts = progress.decode().split("|")
        return {
            "status": parts[0],          # running | done | error
            "checked": int(parts[1]),
            "with_comments": int(parts[2]),
            "remaining": int(parts[3]),
            "current": parts[4] if len(parts) > 4 else "",
        }
    except Exception:
        return {"status": "idle"}


@router.post("/verify/stop")
async def stop_verify_comments(
    current_user: User = Depends(get_current_user),
):
    """Прерывает текущую проверку."""
    import redis as redis_lib
    import os
    r = redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    r.setex(f"parser:verify:stop:{current_user.id}", 300, "1")
    return {"status": "stopping"}


# ══════════════════════════════════════════════════════════
# Alive Check — был ли пост за последние N дней
# ══════════════════════════════════════════════════════════

class AliveCheckRequest(BaseModel):
    account_id: Optional[int] = None  # только для логов — таск работает через web (t.me/s/), не через API
    folder: str = ""
    limit: int = 200
    max_days_inactive: int = 30  # каналы без поста дольше этого — «мёртвые»
    pause_min: float = 0.3
    pause_max: float = 0.8


@router.post("/alive/start")
async def start_alive_check(
    body: AliveCheckRequest,
    current_user: User = Depends(get_current_user),
):
    """
    Проверяет дату последнего поста для каналов в папке через web (t.me/s/{username}).
    НЕ дёргает Telegram API — никакого риска флуда/бана. account_id опционален и
    используется только для логирования.
    """
    from celery_app import celery_app
    task = celery_app.send_task(
        "tasks.parser_similar_tasks.run_alive_check",
        args=[current_user.id, body.account_id or 0, body.model_dump()],
        queue="ai_dialogs",
    )
    return {
        "task_id": task.id,
        "status": "started",
        "message": f"Проверка живости запущена (лимит {body.limit}, порог {body.max_days_inactive} дн.)",
    }


@router.get("/alive/progress")
async def get_alive_progress(
    current_user: User = Depends(get_current_user),
):
    """Прогресс проверки живости."""
    import redis as redis_lib
    import os
    r = redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    progress = r.get(f"parser:alive:progress:{current_user.id}")
    if not progress:
        return {"status": "idle"}
    try:
        parts = progress.decode().split("|")
        return {
            "status": parts[0],
            "checked": int(parts[1]),
            "alive": int(parts[2]),
            "remaining": int(parts[3]),
            "current": parts[4] if len(parts) > 4 else "",
        }
    except Exception:
        return {"status": "idle"}


@router.post("/alive/stop")
async def stop_alive_check(
    current_user: User = Depends(get_current_user),
):
    """Прерывает проверку живости."""
    import redis as redis_lib
    import os
    r = redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    r.setex(f"parser:alive:stop:{current_user.id}", 300, "1")
    return {"status": "stopping"}


# ══════════════════════════════════════════════════════════
# Keyword Expander — расширение ключевых слов
# ══════════════════════════════════════════════════════════

class ExpandKeywordsRequest(BaseModel):
    seeds: list[str]                              # исходные keywords
    target_geos: Optional[list[str]] = None       # ["en", "ru", "ua"...] или None = все
    include_translit: bool = True
    include_translations: bool = True
    include_geo_variants: bool = True
    include_prefixes_suffixes: bool = True
    include_topic_synonyms: bool = True
    max_per_seed: int = 100


@router.post("/keywords/expand")
async def expand_keywords_endpoint(
    body: ExpandKeywordsRequest,
    current_user: User = Depends(get_current_user),
):
    """
    Расширяет seed-ключевики в десятки вариантов.
    Возвращает группированный результат: {seed: {category: [keywords...]}}
    """
    from utils.keyword_expander import expand_keywords

    if not body.seeds:
        raise HTTPException(status_code=400, detail="Укажите хотя бы один seed")

    seeds_clean = [s.strip() for s in body.seeds if s.strip()]
    if not seeds_clean:
        raise HTTPException(status_code=400, detail="Все seeds пустые")

    expanded = expand_keywords(
        seeds=seeds_clean,
        target_geos=body.target_geos,
        include_translit=body.include_translit,
        include_translations=body.include_translations,
        include_geo_variants=body.include_geo_variants,
        include_prefixes_suffixes=body.include_prefixes_suffixes,
        include_topic_synonyms=body.include_topic_synonyms,
        max_per_seed=body.max_per_seed,
    )

    # Преобразуем в JSON-friendly формат с группировкой по категориям
    result = {}
    total = 0
    for seed, keywords in expanded.items():
        by_category = {}
        for kw in keywords:
            by_category.setdefault(kw.category, []).append({
                "keyword": kw.keyword,
                "geo": kw.geo,
                "rating": kw.rating,
            })
        result[seed] = {
            "total": len(keywords),
            "categories": by_category,
            "flat": [kw.keyword for kw in keywords],  # плоский список для copy-paste
        }
        total += len(keywords)

    return {
        "seeds_count": len(seeds_clean),
        "total_keywords": total,
        "results": result,
    }


@router.get("/keywords/geos")
async def list_keyword_geos(
    current_user: User = Depends(get_current_user),
):
    """Возвращает доступные гео + готовые пресеты."""
    from utils.keyword_expander import list_available_geos, get_geo_presets
    return {
        "geos": list_available_geos(),
        "presets": get_geo_presets(),
    }

# ══════════════════════════════════════════════════════════
# Parser Metrics / Stats — дашборд парсера (v2, без ParsedChannel.source)
#
# Эти endpoints ЗАМЕНЯЮТ предыдущие /parser/stats/* в api/routers/parser.py
# Удали старые (если уже вставил) и вставь эти.
#
# Требует:
#   from datetime import datetime, timedelta
#   from sqlalchemy import func, case, cast, Date
#   from models.parser_event import ParserEvent
# ══════════════════════════════════════════════════════════


def _classify_source_sql():
    """SQL CASE-выражение: определяет source канала по search_query."""
    from sqlalchemy import case, func
    # similar:@xxx → similar
    # import → import
    # всё остальное → search (telegram)
    return case(
        (func.coalesce(ParsedChannel.search_query, '').like('similar:%'), 'similar'),
        (func.coalesce(ParsedChannel.search_query, '').like('tgstat_web:%'), 'tgstat_web'),
        (func.coalesce(ParsedChannel.search_query, '') == 'import', 'import'),
        else_='search',
    )


@router.get("/stats/overview")
async def get_parser_stats_overview(
    period: str = "all",
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """KPI-метрики парсера: общее + сегодня."""
    from datetime import datetime, timedelta
    from sqlalchemy import func, case
    from models.parser_event import ParserEvent

    # ── общее: каналы в БД ──
    total_q = await db.execute(
        select(func.count(ParsedChannel.id)).where(ParsedChannel.user_id == current_user.id)
    )
    total_channels = total_q.scalar() or 0

    # ── по source (определяем через search_query) ──
    source_case = _classify_source_sql()
    by_src_q = await db.execute(
        select(
            source_case.label("source"),
            func.count(ParsedChannel.id).label("cnt"),
        ).where(ParsedChannel.user_id == current_user.id)
         .group_by(source_case)
    )
    by_source = {row[0]: row[1] for row in by_src_q.fetchall()}

    # ── сегодня: каналы добавленные сегодня ──
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    today_added_q = await db.execute(
        select(func.count(ParsedChannel.id)).where(
            ParsedChannel.user_id == current_user.id,
            ParsedChannel.added_at >= today_start,
        )
    )
    today_added = today_added_q.scalar() or 0

    # ── сегодня: FLOOD события ──
    flood_q = await db.execute(
        select(
            func.count(ParserEvent.id),
            func.coalesce(func.sum(ParserEvent.wait_seconds), 0),
        ).where(
            ParserEvent.user_id == current_user.id,
            ParserEvent.event_type == "flood_wait",
            ParserEvent.created_at >= today_start,
        )
    )
    row = flood_q.fetchone()
    today_flood_events = row[0] or 0
    today_flood_wait_sum = int(row[1] or 0)

    # ── Avg speed (каналов/мин) — из последних 10 session_done сегодня ──
    speed_q = await db.execute(
        select(ParserEvent.channels_saved, ParserEvent.duration_sec).where(
            ParserEvent.user_id == current_user.id,
            ParserEvent.event_type == "session_done",
            ParserEvent.created_at >= today_start,
            ParserEvent.duration_sec > 0,
            ParserEvent.channels_saved > 0,
        ).order_by(ParserEvent.created_at.desc()).limit(10)
    )
    rows = speed_q.fetchall()
    if rows:
        total_saved = sum(r[0] for r in rows)
        total_minutes = sum(r[1] for r in rows) / 60
        avg_speed = round(total_saved / total_minutes, 1) if total_minutes > 0 else 0
    else:
        avg_speed = 0

    # ── Всего FLOOD за всё время ──
    total_flood_q = await db.execute(
        select(func.count(ParserEvent.id)).where(
            ParserEvent.user_id == current_user.id,
            ParserEvent.event_type == "flood_wait",
        )
    )
    total_flood = total_flood_q.scalar() or 0

    return {
        "total_channels": total_channels,
        "today_added": today_added,
        "today_flood_events": today_flood_events,
        "today_flood_wait_seconds": today_flood_wait_sum,
        "total_flood_events": total_flood,
        "avg_speed_per_min": avg_speed,
        "by_source": by_source,
    }


@router.get("/stats/activity")
async def get_parser_activity(
    days: int = 7,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """График активности: сколько каналов добавлено в БД по дням."""
    from datetime import datetime, timedelta
    from sqlalchemy import func, cast, Date

    cutoff = datetime.utcnow() - timedelta(days=days)
    q = await db.execute(
        select(
            cast(ParsedChannel.added_at, Date).label("day"),
            func.count(ParsedChannel.id),
        ).where(
            ParsedChannel.user_id == current_user.id,
            ParsedChannel.added_at >= cutoff,
        ).group_by("day").order_by("day")
    )

    by_day = {row[0].isoformat(): row[1] for row in q.fetchall()}
    result = []
    today = datetime.utcnow().date()
    for i in range(days, -1, -1):
        d = today - timedelta(days=i)
        result.append({
            "date": d.isoformat(),
            "count": by_day.get(d.isoformat(), 0),
        })

    return {"days": result}


@router.get("/stats/flood-events")
async def get_flood_events(
    limit: int = 20,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Последние FLOOD_WAIT события."""
    from models.parser_event import ParserEvent

    q = await db.execute(
        select(ParserEvent).where(
            ParserEvent.user_id == current_user.id,
            ParserEvent.event_type == "flood_wait",
        ).order_by(ParserEvent.created_at.desc()).limit(limit)
    )
    events = q.scalars().all()

    return [
        {
            "id": e.id,
            "source": e.source,
            "wait_seconds": e.wait_seconds,
            "seed": e.seed,
            "details": e.details,
            "account_id": e.account_id,
            "created_at": e.created_at.isoformat() + "Z",
        }
        for e in events
    ]


@router.get("/stats/top-seeds")
async def get_top_seeds(
    limit: int = 10,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Топ seeds — какие search_query дали больше всего каналов."""
    from sqlalchemy import func

    q = await db.execute(
        select(
            ParsedChannel.search_query,
            func.count(ParsedChannel.id).label("cnt"),
        ).where(
            ParsedChannel.user_id == current_user.id,
            ParsedChannel.search_query != "",
        ).group_by(ParsedChannel.search_query)
         .order_by(func.count(ParsedChannel.id).desc())
         .limit(limit)
    )

    return [
        {"seed": row[0] or "(пусто)", "count": row[1]}
        for row in q.fetchall()
    ]


@router.get("/stats/by-account")
async def get_stats_by_account(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Сколько каналов нашёл каждый аккаунт (через parser_events.account_id)."""
    from sqlalchemy import func
    from models.parser_event import ParserEvent

    q = await db.execute(
        select(
            ParserEvent.account_id,
            func.coalesce(func.sum(ParserEvent.channels_saved), 0).label("saved"),
            func.coalesce(func.sum(ParserEvent.channels_found), 0).label("found"),
            func.count(ParserEvent.id).label("sessions"),
        ).where(
            ParserEvent.user_id == current_user.id,
            ParserEvent.event_type == "session_done",
            ParserEvent.account_id.isnot(None),
        ).group_by(ParserEvent.account_id)
         .order_by(func.sum(ParserEvent.channels_saved).desc())
    )

    rows = q.fetchall()
    if not rows:
        return []

    acc_ids = [r[0] for r in rows]
    acc_q = await db.execute(
        select(TelegramAccount).where(TelegramAccount.id.in_(acc_ids))
    )
    acc_map = {a.id: a for a in acc_q.scalars().all()}

    result = []
    for r in rows:
        acc = acc_map.get(r[0])
        result.append({
            "account_id": r[0],
            "name": (acc.first_name or acc.phone or f"#{r[0]}") if acc else f"#{r[0]}",
            "saved": int(r[1]),
            "found": int(r[2]),
            "sessions": int(r[3]),
        })
    return result


@router.get("/stats/sessions")
async def get_recent_sessions(
    limit: int = 20,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Последние завершённые сессии парсинга."""
    from models.parser_event import ParserEvent

    q = await db.execute(
        select(ParserEvent).where(
            ParserEvent.user_id == current_user.id,
            ParserEvent.event_type == "session_done",
        ).order_by(ParserEvent.created_at.desc()).limit(limit)
    )
    events = q.scalars().all()

    return [
        {
            "id": e.id,
            "source": e.source,
            "account_id": e.account_id,
            "found": e.channels_found,
            "saved": e.channels_saved,
            "duration_sec": e.duration_sec,
            "speed_per_min": round(e.channels_saved / (e.duration_sec / 60), 1) if e.duration_sec > 0 else 0,
            "details": e.details,
            "created_at": e.created_at.isoformat() + "Z",
        }
        for e in events
    ]

# ══════════════════════════════════════════════════════════
# Language Detector
# ══════════════════════════════════════════════════════════

class DetectLanguageRequest(BaseModel):
    folder: str = ""
    limit: int = 500
    auto_folder: bool = True
    only_unknown: bool = True

@router.post("/language/start")
async def start_detect_language(
    body: DetectLanguageRequest,
    current_user: User = Depends(get_current_user)
):
    from celery_app import celery_app
    task = celery_app.send_task(
        "tasks.parser_similar_tasks.run_detect_language",
        args=[current_user.id, body.model_dump()],
        queue="ai_dialogs",
    )
    return {"task_id": task.id, "status": "started"}

@router.get("/language/progress")
async def get_language_progress(current_user: User = Depends(get_current_user)):
    import redis as redis_lib
    r = redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    progress = r.get(f"parser:lang:progress:{current_user.id}")
    if not progress: return {"status": "idle"}
    
    try:
        parts = progress.decode().split("|")
        return {
            "status": parts[0],
            "checked": int(parts[1]),
            "detected": int(parts[2]),
            "remaining": int(parts[3]),
            "current": parts[4] if len(parts) > 4 else "",
        }
    except Exception:
        return {"status": "idle"}

@router.post("/language/stop")
async def stop_language(current_user: User = Depends(get_current_user)):
    import redis as redis_lib
    r = redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    r.setex(f"parser:lang:stop:{current_user.id}", 300, "1")
    return {"status": "stopping"}


# ═══════════════════════════════════════════════════════════════════
# Web-парсер (Camoufox + пул из ~34 статических IPv4)
# ═══════════════════════════════════════════════════════════════════
# Эндпоинты для отказоустойчивого парсинга публичной статистики со SPA.
# Внутрянка: api/utils/web_scraper (worker pool + cooldown + UX-эмуляция),
# Celery-таск: tasks.web_scraper_tasks.run_web_scraper.
#
# Flow:
#   1. POST /parser/web/scrape/start  — фронт шлёт urls + proxies + options
#      → бэк генерит job_id, ставит таск в queue=parsers, возвращает job_id
#   2. GET /parser/web/scrape/progress?job_id=X — фронт поллит каждые 2 сек
#   3. POST /parser/web/scrape/stop?job_id=X — фронт ставит cancel-флаг
#   4. GET /parser/web/scrape/results?job_id=X — скачать JSONL с результатами

import json as _json
import uuid as _uuid
from pathlib import Path as _Path
from fastapi.responses import FileResponse as _FileResponse


def _ws_progress_key(user_id: int, job_id: str) -> str:
    return f"gramgpt:web_scraper:progress:{user_id}:{job_id}"


def _ws_cancel_key(user_id: int, job_id: str) -> str:
    return f"gramgpt:web_scraper:cancel:{user_id}:{job_id}"


def _ws_task_id_key(user_id: int, job_id: str) -> str:
    """Celery task_id для job'a. Нужен чтобы revoke(terminate=True) при
    force-reset — иначе не знаем какой воркер убить."""
    return f"gramgpt:web_scraper:task_id:{user_id}:{job_id}"


def _ws_jsonl_path(user_id: int, job_id: str) -> _Path:
    api_dir = _Path(__file__).resolve().parent.parent
    return (api_dir / ".." / "data" / "web_scraper" / str(user_id) / f"{job_id}.jsonl").resolve()


class WebScrapeStartRequest(BaseModel):
    urls: list[str]
    proxies: list[str] = []
    # Если pasted-список пуст и use_db_proxies=True — берём из БД юзера.
    use_db_proxies: bool = True
    only_valid_proxies: bool = True
    # Все опции необязательны — есть безопасные дефолты в Celery-таске
    max_workers: Optional[int] = 3
    max_retries: Optional[int] = 3
    page_timeout_sec: Optional[float] = 60.0
    cooldown_min_sec: Optional[int] = 900
    cooldown_max_sec: Optional[int] = 1200
    node_rotation_min_sec: Optional[float] = 15.0
    node_rotation_max_sec: Optional[float] = 35.0
    page_locale: Optional[str] = "en-US"
    humanize: Optional[bool] = True
    headless: Optional[bool] = True
    # cloak    = CloakBrowser с persistent-профилем на каждый прокси
    #            (fingerprint и cookies копятся → меньше CF-челленджей). Дефолт.
    # camoufox = patched Firefox с fresh context per URL (fallback, легковеснее)
    engine: Optional[str] = "cloak"


@router.post("/web/scrape/start")
async def web_scrape_start(
    body: WebScrapeStartRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Запускает Celery-таск Camoufox-скрейпинга. Возвращает job_id."""
    from models.proxy import Proxy

    urls = [u.strip() for u in body.urls if u and u.strip()]
    proxies = [p.strip() for p in body.proxies if p and p.strip()]
    proxies_source = "manual" if proxies else "db"

    if not proxies and body.use_db_proxies:
        q = select(Proxy).where(Proxy.user_id == current_user.id)
        if body.only_valid_proxies:
            q = q.where(Proxy.is_valid == True)
        for p in (await db.execute(q)).scalars().all():
            scheme = p.protocol.value if hasattr(p.protocol, "value") else str(p.protocol)
            if p.login and p.password:
                proxies.append(f"{scheme}://{p.login}:{p.password}@{p.host}:{p.port}")
            else:
                proxies.append(f"{scheme}://{p.host}:{p.port}")

    if not urls:
        raise HTTPException(status_code=400, detail="urls пустой")
    if len(proxies) < 3:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Минимум 3 прокси (найдено {len(proxies)}, источник={proxies_source}). "
                "Добавь прокси в проект или вставь их вручную."
            ),
        )

    job_id = _uuid.uuid4().hex

    options = {
        "max_workers": max(1, min(int(body.max_workers or 3), 6)),
        "max_retries": max(1, min(int(body.max_retries or 3), 5)),
        "page_timeout_sec": float(body.page_timeout_sec or 60.0),
        "cooldown_min_sec": int(body.cooldown_min_sec or 900),
        "cooldown_max_sec": int(body.cooldown_max_sec or 1200),
        "node_rotation_min_sec": float(body.node_rotation_min_sec or 15.0),
        "node_rotation_max_sec": float(body.node_rotation_max_sec or 35.0),
        "page_locale": body.page_locale or "en-US",
        "engine": (body.engine or "cloak").lower(),
        "camoufox": {
            "humanize": bool(body.humanize),
            "headless": bool(body.headless),
        },
        "cloak": {
            "humanize": bool(body.humanize),
            "headless": bool(body.headless),
        },
    }

    from celery_app import celery_app
    task = celery_app.send_task(
        "tasks.web_scraper_tasks.run_web_scraper",
        args=[current_user.id, job_id, urls, proxies, options],
        queue="parsers",
    )

    # Сразу публикуем initial progress, чтобы фронт не получил пустоту на
    # первом поллинге, пока Celery поднимает Camoufox.
    import redis as _redis_lib
    r = _redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    initial = {
        "status": "queued",
        "job_id": job_id,
        "urls_total": len(urls),
        "proxies_total": len(proxies),
        "ts": int(__import__("time").time()),
    }
    r.setex(_ws_progress_key(current_user.id, job_id), 86400,
            _json.dumps(initial, ensure_ascii=False))
    # Сохраняем celery task_id — reset endpoint использует revoke(terminate=True)
    r.setex(_ws_task_id_key(current_user.id, job_id), 86400, task.id)

    return {
        "job_id": job_id,
        "task_id": task.id,
        "urls_total": len(urls),
        "proxies_total": len(proxies),
        "proxies_source": proxies_source,
        "status": "queued",
    }


@router.get("/web/scrape/progress")
async def web_scrape_progress(
    job_id: str = Query(...),
    current_user: User = Depends(get_current_user),
):
    """Текущий snapshot прогресса. Фронт поллит каждые 2 сек."""
    import redis as _redis_lib
    r = _redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    raw = r.get(_ws_progress_key(current_user.id, job_id))
    if not raw:
        return {"status": "unknown", "job_id": job_id}
    try:
        return _json.loads(raw)
    except Exception:
        return {"status": "unknown", "job_id": job_id, "raw": raw.decode("utf-8", errors="ignore")}


@router.post("/web/scrape/stop")
async def web_scrape_stop(
    job_id: str = Query(...),
    current_user: User = Depends(get_current_user),
):
    """
    Выставляет cancel-флаг. WebScraper.cancel_check() читает его раз
    в ~0.5-2 сек, после чего воркеры завершают текущий URL и выходят.
    """
    import redis as _redis_lib
    r = _redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    r.setex(_ws_cancel_key(current_user.id, job_id), 3600, "1")
    return {"status": "cancelling", "job_id": job_id}


@router.post("/web/scrape/reset")
async def web_scrape_reset(
    job_id: str = Query(...),
    current_user: User = Depends(get_current_user),
):
    """
    Force-очистка зависшего job'а:
      1. Ревокает Celery-таску через revoke(terminate=True) — SIGTERM в
         воркер-тред. Убивает даже застрявший goto(). Требует чтобы
         task_id был сохранён в Redis (для job'ов запущенных ДО этого
         фикса task_id отсутствует — тогда просто чистим Redis-ключи,
         фоновая таска умрёт при следующем stop_all.ps1).
      2. Удаляет ключи progress, cancel, task_id из Redis — UI сразу
         видит "unknown" статус и стряхивает jobId с localStorage.
      3. JSONL на диске не трогается — уже собранное сохранено.

    Endpoint для случая когда обычный Stop не помог (celery worker
    завис в blocking pipe read из Chromium-subprocess, либо запущен
    старый код без emergency_pool_stop).
    """
    import redis as _redis_lib
    r = _redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    task_key = _ws_task_id_key(current_user.id, job_id)
    revoked = False
    task_id_bytes = r.get(task_key)
    if task_id_bytes:
        task_id = task_id_bytes.decode() if isinstance(task_id_bytes, bytes) else str(task_id_bytes)
        try:
            from celery_app import celery_app
            # terminate=True → SIGTERM в тред-воркера, signal='SIGKILL' для
            # решительной пришибки. На Windows Celery threads поддерживают
            # только TERM (SIGKILL игнорится), но и того обычно хватает.
            celery_app.control.revoke(task_id, terminate=True, signal="SIGTERM")
            revoked = True
            logging.getLogger(__name__).info(
                f"[web_scrape_reset] revoked celery task {task_id} for job {job_id}"
            )
        except Exception as e:
            logging.getLogger(__name__).warning(
                f"[web_scrape_reset] revoke failed for {task_id}: {e}"
            )
    # В любом случае чистим Redis — UI перестанет видеть "running"
    r.delete(
        _ws_progress_key(current_user.id, job_id),
        _ws_cancel_key(current_user.id, job_id),
        task_key,
    )
    return {
        "status": "reset",
        "job_id": job_id,
        "revoked_task": revoked,
        "hint": (
            "Если revoked=False — task_id не был сохранён (старый job). "
            "UI очищен, но фоновая таска (если ещё живая) продолжит крутиться "
            "до stop_all.ps1."
        ),
    }


@router.get("/web/scrape/results")
async def web_scrape_results(
    job_id: str = Query(...),
    current_user: User = Depends(get_current_user),
):
    """Скачивает JSONL с результатами. Содержит даже частично записанные строки."""
    path = _ws_jsonl_path(current_user.id, job_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Файл результатов не найден")
    return _FileResponse(
        str(path),
        media_type="application/x-ndjson",
        filename=f"web_scrape_{job_id}.jsonl",
    )


@router.get("/web/scrape/log-tail")
async def web_scrape_log_tail(
    job_id: str = Query(...),
    lines: int = Query(30, ge=1, le=500),
    current_user: User = Depends(get_current_user),
):
    """
    Возвращает последние N строк из per-job лога — для live viewer в UI.
    Дёшево (memory-efficient tail через seek), 3-сек polling из UI ok.
    """
    api_dir = _Path(__file__).resolve().parent.parent
    path = (api_dir / ".." / "logs" / "web_scraper" / str(current_user.id) / f"{job_id}.log").resolve()
    if not path.exists():
        return {"lines": [], "size_bytes": 0, "exists": False}
    # Простой tail — читаем последние ~lines*250 байт (типичная строка 100-200 chars)
    size = path.stat().st_size
    read_size = min(size, lines * 400)
    with path.open("rb") as f:
        f.seek(max(0, size - read_size))
        chunk = f.read()
    try:
        text = chunk.decode("utf-8", errors="replace")
    except Exception:
        text = str(chunk)
    all_lines = text.splitlines()
    return {
        "lines": all_lines[-lines:],
        "size_bytes": size,
        "exists": True,
    }


@router.get("/web/scrape/log")
async def web_scrape_log(
    job_id: str = Query(...),
    current_user: User = Depends(get_current_user),
):
    """
    Скачивает per-job log с полной трассой скрейпера — env, movements
    прокси, launch/restart браузера, ошибки. То что юзер должен прислать
    разработчику при "у меня что-то сломалось".

    Файл: logs/web_scraper/{user_id}/{job_id}.log
    """
    api_dir = _Path(__file__).resolve().parent.parent
    path = (api_dir / ".." / "logs" / "web_scraper" / str(current_user.id) / f"{job_id}.log").resolve()
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail="Лог не найден. Возможно job слишком старый или запускался до появления логирования.",
        )
    return _FileResponse(
        str(path),
        media_type="text/plain; charset=utf-8",
        filename=f"web_scrape_{job_id}.log",
    )


@router.get("/web/scrape/jobs")
async def web_scrape_list_jobs(current_user: User = Depends(get_current_user)):
    """
    Список последних job'ов юзера — по содержимому data/web_scraper/{user_id}/.
    Для UI "история запусков".
    """
    api_dir = _Path(__file__).resolve().parent.parent
    user_dir = (api_dir / ".." / "data" / "web_scraper" / str(current_user.id)).resolve()
    if not user_dir.exists():
        return {"jobs": []}
    jobs = []
    for f in sorted(user_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:50]:
        try:
            stat = f.stat()
            # Считаем строки = записи. Дёшево — файл максимум на 10к URL.
            with f.open("r", encoding="utf-8") as fh:
                count = sum(1 for _ in fh)
            jobs.append({
                "job_id": f.stem,
                "records": count,
                "size_bytes": stat.st_size,
                "modified_at": int(stat.st_mtime),
            })
        except Exception:
            continue
    return {"jobs": jobs}


# ── TGStat preset ──────────────────────────────────────────────────
# Удобная обёртка над web/scrape/start для TGStat: вместо ручного
# составления URL'ов фронт шлёт {languages, categories, only_with_comments,
# pages_per_geo}, бэк сам генерит список URL через build_urls() и запускает
# таску с extractor="tgstat".


@router.get("/web/scrape/tgstat/options")
async def web_scrape_tgstat_options(current_user: User = Depends(get_current_user)):
    """Справочник для UI: какие гео и категории доступны на TGStat."""
    from utils.web_scraper import GEO_COUNTRIES
    from utils.web_scraper.tgstat_urls import GEO_CATEGORIES
    return {
        "languages": GEO_COUNTRIES,   # legacy
        "categories": GEO_CATEGORIES,
        "countries": GEO_COUNTRIES,
        "targets": [
            {"key": "channels", "label": "📢 Каналы (рейтинг)"},
            {"key": "chats",    "label": "💬 Чаты с комментами (рейтинг discussion-групп)"},
        ],
    }


class WebScrapeTgstatRequest(BaseModel):
    # Если pasted_proxies пустой — берём ВСЕ валидные прокси юзера из БД.
    # use_db_proxies=True (дефолт) принудительно использует БД и игнорит
    # paste. Это удобно когда у юзера уже есть пул в проге.
    proxies: list[str] = []             # legacy: ручной paste
    use_db_proxies: bool = True
    only_valid_proxies: bool = True     # отфильтровать невалидные (is_valid=False)
    languages: list[str] = []           # legacy, не используется
    # slugs категорий TGStat (crypto, business, games, ...). Если задано —
    # каждая категория комбинируется с каждой страной (или global если
    # стран нет). N категорий × M стран = N*M rating-URL'ов = ~N*M*100
    # уникальных кандидатов.
    categories: list[str] = []
    countries: list[str] = []           # ISO-2 коды (RU, UA, KZ...) или пусто = глобально
    target: str = "channels"            # дефолт: каналы (потом фильтр через детали)
    only_with_comments: bool = True
    pages_per_geo: int = 1              # сколько страниц с каждого среза
    include_global: bool = False        # добавлять глобальный URL (без country)
    # Источник кандидатов:
    #   "rating"  — топ-100 из /ratings/channels{/category}?sort=members. Дефолт.
    #                Ограничено ~100 на URL, но качественные каналы.
    #   "sitemap" — TGStat sitemap.xml (~10k global + ~10k на каждый geo).
    #                Огромный охват, но качество каналов смешанное — Stage 2
    #                отфильтрует по linked_chat.
    source: str = "rating"
    # Сколько кандидатов проверить во 2-м проходе (детальные страницы).
    # 0 = без лимита. Влияет на ВРЕМЯ: каждый кандидат это 1 HTTP запрос
    # к /channel/@xxx с 60-120 сек cooldown между прокси.
    max_channels: int = 30
    # Дедуп: пропускать каналы уже проверенные за N дней.
    # 0 = не пропускать (пересканить всё). Дефолт 30.
    skip_recently_checked_days: Optional[int] = 30

    max_workers: Optional[int] = 3
    max_retries: Optional[int] = 3
    page_timeout_sec: Optional[float] = 60.0
    cooldown_min_sec: Optional[int] = 900
    cooldown_max_sec: Optional[int] = 1200
    node_rotation_min_sec: Optional[float] = 15.0
    node_rotation_max_sec: Optional[float] = 35.0
    page_locale: Optional[str] = "en-US"
    humanize: Optional[bool] = True
    headless: Optional[bool] = True
    engine: Optional[str] = "cloak"


@router.post("/web/scrape/tgstat/start")
async def web_scrape_tgstat_start(
    body: WebScrapeTgstatRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Запуск Camoufox-скрейпинга TGStat по выбранным гео/таргету."""
    from utils.web_scraper import build_urls, sitemap_urls_for
    from models.proxy import Proxy

    # ── Сборка прокси-пула ─────────────────────────────────────
    # Приоритет:
    #   1. Если pasted proxies non-empty → используем их (ручной paste)
    #   2. Иначе если use_db_proxies → берём из БД
    proxies: list[str] = []
    proxies_source = "manual"

    pasted = [p.strip() for p in body.proxies if p and p.strip()]
    if pasted:
        proxies = pasted
    elif body.use_db_proxies:
        proxies_source = "db"
        q = select(Proxy).where(Proxy.user_id == current_user.id)
        if body.only_valid_proxies:
            q = q.where(Proxy.is_valid == True)
        db_proxies = (await db.execute(q)).scalars().all()
        for p in db_proxies:
            scheme = p.protocol.value if hasattr(p.protocol, "value") else str(p.protocol)
            if p.login and p.password:
                proxies.append(f"{scheme}://{p.login}:{p.password}@{p.host}:{p.port}")
            else:
                proxies.append(f"{scheme}://{p.host}:{p.port}")

    if len(proxies) < 3:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Минимум 3 прокси (найдено {len(proxies)}, источник={proxies_source}). "
                "Добавь прокси в проект или вставь их вручную."
            ),
        )

    # Если фронт прислал старое поле "languages" вместо нового "countries" —
    # принимаем как страны (фронт ещё мог не обновиться). И наоборот.
    countries = body.countries or body.languages or []
    target = (body.target or "chats").lower()
    if target not in ("channels", "chats"):
        target = "chats"

    source = (body.source or "rating").lower()
    if source not in ("rating", "sitemap"):
        source = "rating"

    if source == "sitemap":
        # Country codes для sitemap иначе называются:
        # ISO-2 из countries маппим в ISO-2/3 кода-ключи SITEMAP_COUNTRIES.
        # У нас в GEO_COUNTRIES codes RU/UA/BY/KZ/UZ/KG/IR/CN/IN/ET, а в
        # SITEMAP_COUNTRIES ключи: ru/ua/by/kz/uz/kg/ir/cn/in/et — то же
        # самое в lowercase. Мапим 1:1.
        cc_lower = [c.lower() for c in countries]
        urls = sitemap_urls_for(
            countries=cc_lower,
            include_global=body.include_global or not countries,
        )
        extractor_name = "tgstat_sitemap"
    else:
        urls = build_urls(
            countries=countries,
            categories=body.categories,
            only_with_comments=body.only_with_comments,
            pages_per_geo=max(1, min(body.pages_per_geo, 10)),
            include_global=body.include_global or not countries,
            target=target,
        )
        extractor_name = "tgstat"

    if not urls:
        raise HTTPException(
            status_code=400,
            detail="Не получилось собрать ни одного URL — выбери хотя бы одну страну или включи глобальный.",
        )

    job_id = _uuid.uuid4().hex
    options = {
        "extractor": extractor_name,
        "max_workers": max(1, min(int(body.max_workers or 3), 6)),
        "max_retries": max(1, min(int(body.max_retries or 3), 5)),
        "page_timeout_sec": float(body.page_timeout_sec or 60.0),
        "cooldown_min_sec": int(body.cooldown_min_sec or 900),
        "cooldown_max_sec": int(body.cooldown_max_sec or 1200),
        "node_rotation_min_sec": float(body.node_rotation_min_sec or 15.0),
        "node_rotation_max_sec": float(body.node_rotation_max_sec or 35.0),
        "page_locale": body.page_locale or "en-US",
        # Лимит кандидатов на проверку во 2-м проходе. 0 = весь Stage 1.
        "stage2_limit": max(0, min(int(body.max_channels or 0), 2000)),
        "skip_recently_checked_days": max(0, int(body.skip_recently_checked_days or 0)),
        "engine": (body.engine or "cloak").lower(),
        "camoufox": {
            "humanize": bool(body.humanize),
            "headless": bool(body.headless),
        },
        "cloak": {
            "humanize": bool(body.humanize),
            "headless": bool(body.headless),
        },
    }

    from celery_app import celery_app
    task = celery_app.send_task(
        "tasks.web_scraper_tasks.run_web_scraper",
        args=[current_user.id, job_id, urls, proxies, options],
        queue="parsers",
    )

    import redis as _redis_lib
    r = _redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    # target = сколько каналов проверим в Stage 2 (то что юзер выставил).
    # UI использует это как основной знаменатель прогресс-бара с самого
    # старта, а не Stage 1 URL count (обычно 1-8), иначе показывает "0/1"
    # что вводит в заблуждение.
    stage2_target = max(0, min(int(body.max_channels or 0), 2000))
    initial = {
        "status": "queued",
        "job_id": job_id,
        "urls_total": len(urls),
        "stage2_target": stage2_target,
        "total": stage2_target,   # UI приоритет total > urls_total
        "proxies_total": len(proxies),
        "preset": "tgstat",
        "languages": body.languages,
        "categories": body.categories,
        "ts": int(__import__("time").time()),
    }
    r.setex(_ws_progress_key(current_user.id, job_id), 86400,
            _json.dumps(initial, ensure_ascii=False))
    # Сохраняем celery task_id для reset endpoint (revoke terminate=True)
    r.setex(_ws_task_id_key(current_user.id, job_id), 86400, task.id)

    return {
        "job_id": job_id,
        "task_id": task.id,
        "urls_total": len(urls),
        "proxies_total": len(proxies),
        "proxies_source": proxies_source,
        "preview_urls": urls[:5],
        "status": "queued",
    }


@router.get("/web/scrape/tgstat/aggregate")
async def web_scrape_tgstat_aggregate(
    job_id: str = Query(...),
    only_with_comments: bool = Query(True),
    min_subscribers: int = Query(0),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Отдаёт список найденных каналов для job'a.

    Источник истины: {job_id}.stage2.jsonl — там per-URL детальные записи
    Stage 2 (channel_username, has_open_comments, subscribers, title,
    linked_chat_username, category, verified). JSONL is per-job by design,
    поэтому корректно возвращает каналы даже для СТАРЫХ job'ов.

    Раньше читали из ParsedChannel по search_query=tgstat_web:{job_id} —
    неверно: UPSERT в web_scraper_tasks.py перезаписывает search_query
    при повторной находке того же канала другим job'ом. Клик 🔍 на
    старый job'ик возвращал 0 каналов, хотя в JSONL они были.

    Из БД подтягиваем language (в Stage 2 JSONL его нет).
    """
    stage2_path = _ws_jsonl_path(current_user.id, job_id).parent / f"{job_id}.stage2.jsonl"

    # Дедуп по username: если один канал попал в JSONL дважды (retry),
    # берём последнюю запись — там актуальный статус.
    by_username: dict[str, dict] = {}
    rows_read_raw = 0
    dead_count = 0
    if stage2_path.exists():
        try:
            with stage2_path.open("r", encoding="utf-8") as f:
                for line in f:
                    rows_read_raw += 1
                    try:
                        rec = _json.loads(line)
                    except Exception:
                        continue
                    if not rec.get("success"):
                        continue
                    data = rec.get("data") or {}
                    u = (data.get("channel_username") or "").lstrip("@")
                    # Fallback: extractor часто не заполняет channel_username
                    # для chat-based candidates, но URL мы знаем —
                    # https://tgstat.com/channel/@X → достаём @X.
                    if not u:
                        url = rec.get("url") or ""
                        marker = "/channel/@"
                        idx = url.find(marker)
                        if idx >= 0:
                            u = url[idx + len(marker):].split("/")[0].split("?")[0].lstrip("@")
                    if not u:
                        continue
                    # ── Фильтр 404-страниц TGStat ──
                    # extractor выставляет is_dead=True когда TGStat отдал
                    # fallback-страницу «канал не найден». В таких записях
                    # linked_chat и subscribers могут быть из кеша, но
                    # реальный Telegram-канал удалён — не показываем.
                    if bool(data.get("is_dead")):
                        dead_count += 1
                        continue
                    has_c = bool(data.get("has_open_comments"))
                    subs = int(data.get("subscribers") or 0)
                    by_username[u.lower()] = {
                        "username": u,
                        "title": (data.get("title") or "")[:250] or None,
                        "subscribers": subs,
                        "has_comments": has_c,
                        "category": data.get("category"),
                        "verified": bool(data.get("verified")),
                        "discussion_chat": (data.get("linked_chat_username") or "").lstrip("@") or None,
                    }
        except Exception as e:
            logging.getLogger(__name__).warning(
                f"[aggregate] Stage 2 JSONL read error for {job_id}: {e}"
            )
    channels_data = list(by_username.values())

    # Language подтягиваем из БД одним запросом по usernames
    lang_by_user: dict[str, str | None] = {}
    if channels_data:
        usernames = [c["username"] for c in channels_data]
        rows = (await db.execute(
            select(ParsedChannel.username, ParsedChannel.language).where(
                ParsedChannel.user_id == current_user.id,
                ParsedChannel.username.in_(usernames),
            )
        )).all()
        lang_by_user = {r[0].lstrip("@"): r[1] for r in rows}

    # Fallback для старых job'ов (до появления Stage 2): читаем из БД
    # по search_query. Аккуратно: это может вернуть уже "переприсвоенные"
    # каналы, но лучше чем ничего.
    if not channels_data:
        rows_fallback = (await db.execute(
            select(ParsedChannel).where(
                ParsedChannel.user_id == current_user.id,
                ParsedChannel.search_query == f"tgstat_web:{job_id}",
            )
        )).scalars().all()
        for r in rows_fallback:
            channels_data.append({
                "username": (r.username or "").lstrip("@"),
                "title": r.title,
                "subscribers": r.subscribers or 0,
                "has_comments": bool(r.has_comments),
                "category": None,
                "verified": False,
                "discussion_chat": None,
            })
            lang_by_user.setdefault((r.username or "").lstrip("@"), r.language)

    # Финальные фильтры + презентация
    out = []
    for c in channels_data:
        if only_with_comments and not c["has_comments"]:
            continue
        if min_subscribers and c["subscribers"] < min_subscribers:
            continue
        u = c["username"]
        out.append({
            "username": "@" + u,
            "title": c["title"],
            "subscribers": c["subscribers"],
            "has_comments": c["has_comments"],
            "category": c["category"],
            "verified": c["verified"],
            "discussion_chat": c["discussion_chat"],
            "language": lang_by_user.get(u),
            "source_url": f"https://tgstat.com/channel/@{u}",
        })
    out.sort(key=lambda x: x.get("subscribers") or 0, reverse=True)
    # Метрики:
    #   rows_read      — сколько уникальных живых каналов прочитали из JSONL
    #                    (после дедупа по username + фильтра is_dead)
    #   channels_count — сколько ПОПАЛО в out после only_with_comments/min_subs фильтров
    #   with_comments  — сколько всего живых с комментами (независимо от фильтра)
    #   dead_count     — сколько 404/dead записей отфильтровали
    #   rows_read_raw  — сырые строки в JSONL (до дедупа)
    with_c = sum(1 for c in channels_data if c["has_comments"])
    without_c = sum(1 for c in channels_data if not c["has_comments"])
    return {
        "job_id": job_id,
        "rows_read": len(channels_data),        # уникальные живые
        "rows_read_raw": rows_read_raw,          # все строки JSONL
        "dead_count": dead_count,
        "with_comments_count": with_c,
        "without_comments_count": without_c,
        "channels_count": len(out),
        "channels": out,
        "source": "stage2_jsonl" if stage2_path.exists() else "db_fallback",
        "skipped_no_guess": 0,
    }