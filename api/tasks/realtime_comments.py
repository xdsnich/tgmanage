"""
ufo.gram — tasks/realtime_comments.py
Комментирование по факту выхода поста: успеть написать первым.

Зачем отдельная система. Плановый комментинг (plan_executor) решает,
КОГДА аккаунту выходить на связь, и пишет в тот пост, который на тот
момент оказался свежайшим. Пост мог выйти пять часов назад, и под ним
уже сорок комментов — наш утонет внизу. Первые комменты видит каждый,
кто открыл обсуждение; это тот же довод, по которому реакции мы ставим
на первые четыре коммента треда, а не на последние.

Здесь наоборот: сначала событие (вышел пост), потом действие.

Как устроено.

  1. НАБЛЮДАТЕЛЬ (watch_realtime_campaigns) раз в N секунд обходит
     каналы realtime-кампаний через ПУБЛИЧНУЮ веб-версию t.me/s/{канал}.
     Аккаунты и их лимиты тут не тратятся вовсе — это обычный HTTP.
     Замер 08.09 через один прокси: 7680 запросов, из них 4800 подряд
     со скоростью 20 в секунду — ни одного 429. То есть обход двух тысяч
     каналов раз в минуту стоит четверти одного адреса.

     Новый пост определяем по номеру: у target_channels уже есть поле
     last_post_id, сравниваем с ним.

  2. ОТБОР АККАУНТА. Пишет тот, кто может: состоит в кампании, активен,
     не отдыхает, не под флудом, не выбрал суточную норму и чей прокси
     сейчас свободен. Предпочитаем того, кто уже вступил в обсуждение
     этого канала — ему не придётся тратить резолв на вступление.

  3. ОЧЕРЕДЬ. Задание кладём в comment_queue (та же таблица, что у
     старого движка) со scheduled_at = сейчас + задержка. Задержка
     нужна: коммент через три секунды после поста — подпись бота.
     Минуту-пять человек пишет вполне естественно, и в первую тройку
     мы при этом попадаем всё равно.

     Очередь НЕ копится. Каналов кратно больше, чем мы успеваем
     обслужить, и задание всё равно выстрелит не раньше, чем
     освободится адрес аккаунта, — до десяти минут. Поэтому аккаунт
     держит одно задание, и при выходе более свежего поста оно
     переписывается на него прямо на месте (_supersede), а не встаёт
     рядом в очередь. К моменту, когда адрес освободится, под рукой
     оказывается самый свежий пост из всех, что вышли за это время.

  4. ИСПОЛНЕНИЕ (dispatch_realtime) отправляет готовые задания на
     исполнение БЕЗ размазывания по часу, которое делает старый
     dispatch_comments — здесь скорость и есть смысл затеи. Разбор идёт
     от новых к старым, протухшее (STALE_MIN) отбраковывается заранее.

Что НЕ трогаем: plan_executor, планы, старые кампании. Realtime-кампания
отличается только полем campaign_type='realtime', всё остальное — те же
таблицы и тот же исполнитель comment_executor.

ENV:
  RT_POLL_SEC            как часто обходить каналы, сек (60)
  RT_DELAY_MIN/MAX       задержка перед комментом, сек (0/25)
  RT_PER_ACCOUNT_DAY     потолок комментов на аккаунт в сутки (40)
  RT_IP_COOLDOWN_SEC     остывание адреса после сессии, сек (120)
  RT_STALE_MIN           через сколько задание протухает, мин (25)
  RT_REPLACE_MARGIN_SEC  насколько новый пост должен быть свежее (120)
  RT_MAX_QUEUE_PER_POST  сколько аккаунтов на один пост (1)
  RT_FETCH_CONCURRENCY   сколько каналов тянуть параллельно (20)
  RT_USE_PROXY           ходить через прокси из базы (1) или напрямую (0)
"""

import asyncio
import os
import random
import re
import sys
import logging
from datetime import datetime, timedelta, timezone

from celery_app import celery_app

logger = logging.getLogger(__name__)
API_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

POLL_SEC = int(os.getenv("RT_POLL_SEC", "60"))
# Своей задержки перед комментом почти нет — она уже встроена в
# исполнителя: тот читает ленту 15-180 секунд и набирает текст ещё 2-25.
# Прежние 60-300 секунд сверху давали в сумме от полутора до восьми минут
# и убивали весь смысл «написать первым».
# Небольшой разброс оставляем, чтобы пачка постов не ушла синхронно.
DELAY_MIN = int(os.getenv("RT_DELAY_MIN", "0"))
DELAY_MAX = int(os.getenv("RT_DELAY_MAX", "25"))
# Через сколько задание протухает: смысл был успеть первым, а коммент
# через полчаса ничем не лучше планового.
STALE_MIN = int(os.getenv("RT_STALE_MIN", "25"))
# Сколько аккаунт не пишет в тот же канал после коммента.
#
# Было три часа, стало один. Причина — замер 09.09 по причинам пропуска
# постов: из ста обходов 93 отказа пришлись именно на «участник недавно
# писал в этот канал». Аккаунт состоит в 14-22 каналах, они кончаются
# быстро, и выходит так, что пост есть, а писать некому.
#
# Час — всё ещё разумный зазор: суточная норма аккаунта (40) размазана
# по полутора десяткам каналов, так что реально в один канал он зайдёт
# два-три раза за день, а не двадцать. Час просто снимает запрет на
# два коммента подряд, который и был исходным смыслом этой паузы.
CH_COOLDOWN_SEC = int(os.getenv("RT_CH_COOLDOWN_SEC", "3600"))
PER_ACCOUNT_DAY = int(os.getenv("RT_PER_ACCOUNT_DAY", "40"))
# Разгон новичка: сколько ему можно в первый день и на сколько
# норма растёт каждые следующие сутки. RT_RAMP_STEP=0 отключает
# разгон совсем — тогда все работают сразу на потолке.
RAMP_START = int(os.getenv("RT_RAMP_START", "12"))
RAMP_STEP = int(os.getenv("RT_RAMP_STEP", "9"))
# Сколько адрес остывает после realtime-сессии. Общий замок держит десять
# минут, и это давало адресу занятость 83%: заданий 20 в час на четыре
# адреса, каждое запирало свой на десять минут. Смысл движка — успеть под
# свежий пост, поэтому здесь остывание короткое.
#
# Защита при этом не слабеет. Полные десять минут по-прежнему отделяют
# РАЗНЫЕ аккаунты на одном адресе — именно эта картина и была причиной
# инцидента 2026-06-12. Быстро возвращается только тот же самый аккаунт,
# за которым адрес закреплён: для Telegram это переподключение одного
# телефона, а не ферма (см. owner в utils/ip_throttle).
RT_IP_COOLDOWN_SEC = int(os.getenv("RT_IP_COOLDOWN_SEC", "120"))
MAX_PER_POST = int(os.getenv("RT_MAX_QUEUE_PER_POST", "1"))
# Сколько заданий на аккаунт может ждать в очереди одновременно.
# Одно — и не из-за пропускной способности, а по смыслу: пока задание
# ждёт своего адреса, выходят более свежие посты, и второе задание
# рядом устарело бы раньше, чем до него дошла очередь. Вместо него
# единственное ожидающее переписывается на свежий пост (_supersede).
MAX_IN_FLIGHT = int(os.getenv("RT_MAX_IN_FLIGHT", "1"))
# Насколько новый пост должен быть свежее ожидающего, чтобы имело смысл
# переписать задание. Без порога перекладка шла бы на каждом обходе.
REPLACE_MARGIN_SEC = int(os.getenv("RT_REPLACE_MARGIN_SEC", "120"))
FETCH_CONCURRENCY = int(os.getenv("RT_FETCH_CONCURRENCY", "20"))
# Сколько адресов крутится в обходе t.me. Это не сессии Telegram, а
# обычные запросы к публичной странице, поэтому замок адресов их не
# касается и с работой аккаунтов они не конкурируют.
FETCH_PROXIES = int(os.getenv("RT_FETCH_PROXIES", "6"))
# Потолок на один запрос к t.me. Без него зависший прокси держит
# слот семафора до победного и стопорит весь обход.
FETCH_TIMEOUT_SEC = int(os.getenv("RT_FETCH_TIMEOUT_SEC", "15"))
USE_PROXY = os.getenv("RT_USE_PROXY", "1") not in ("0", "false", "no")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")

QUOTA_KEY = "gramgpt:rt:quota:{acc}:{day}"
# День, когда аккаунт впервые написал в realtime. От него считается
# разгон нормы — см. _quota_today.
SINCE_KEY = "gramgpt:rt:since:{acc}"
SEEN_KEY = "gramgpt:rt:seen:{camp}:{ch}:{post}"
CH_COOLDOWN_KEY = "gramgpt:rt:chcool:{acc}:{ch}"
# Задание отдано исполнителю. Нужна отдельная метка: статус 'executing'
# исполнитель ставит внутри своей транзакции и держит её до конца работы,
# то есть снаружи задание всё это время выглядит как ожидающее. Без метки
# наблюдатель попробовал бы переписать строку, которую уже комментируют,
# и повис бы на блокировке до конца чужой сессии.
SENT_KEY = "gramgpt:rt:sent:{qid}"
PENDING_KEY = "gramgpt:rt:pending:{camp}"
STATE_KEY = "gramgpt:rt:state"

# Разметка t.me/s/: номер поста и время последнего сообщения.
RE_POST = re.compile(r'data-post="[^/"]+/(\d+)"')
RE_TIME = re.compile(r'<time datetime="([^"]+)"')
RE_TEXT = re.compile(
    r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S)


def run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _redis():
    from utils.redis_pool import get_redis
    return get_redis()


def _quota_today(acc_id: int) -> int:
    """Норма именно этого аккаунта на сегодня, с учётом разгона.

    Потолок PER_ACCOUNT_DAY — для тех, кто уже в деле. Новичку он не
    подходит: аккаунт, который вчера не писал ничего, а сегодня выдал
    сорок комментариев в сорока чужих обсуждениях, выглядит ровно как
    то, что Telegram и ищет. Поэтому первый день — RAMP_START, дальше
    прибавка RAMP_STEP в сутки до потолка.

    Точка отсчёта — день первого коммента, запоминается один раз
    (ключ ставится с nx). Ветеранам он проставлен задним числом по их
    настоящей истории, поэтому разгон их не касается.
    """
    from datetime import date
    if RAMP_STEP <= 0:
        return PER_ACCOUNT_DAY
    try:
        r = _redis()
        ключ = SINCE_KEY.format(acc=acc_id)
        r.set(ключ, date.today().isoformat(), nx=True)
        сырое = r.get(ключ)
        начало = date.fromisoformat(
            сырое.decode() if isinstance(сырое, bytes) else str(сырое))
        дней = max(0, (date.today() - начало).days)
    except Exception:
        дней = 0
    return max(1, min(PER_ACCOUNT_DAY, RAMP_START + RAMP_STEP * дней))


def _quota_left(acc_id: int) -> int:
    """Сколько комментов аккаунту ещё можно сегодня."""
    try:
        from datetime import date
        used = int(_redis().get(
            QUOTA_KEY.format(acc=acc_id, day=date.today().isoformat())) or 0)
        return max(0, _quota_today(acc_id) - used)
    except Exception:
        return PER_ACCOUNT_DAY


def _quota_give_back(acc_id: int, channel: str = ""):
    """Вернуть норму и снять остывание канала.

    Норму мы занимаем при постановке в очередь, чтобы аккаунт не набрал
    заданий сверх суточного потолка. Но если задание не выполнилось —
    протухло в очереди, канал закрылся, LLM не ответила — коммента не
    было, и списывать за него нельзя.

    Замер 08.09: пять аккаунтов выбрали всю норму (50 единиц), а
    комментов вышло семь. 43 сгорели на протухших заданиях, и кампания
    встала до полуночи, ничего не написав.
    """
    try:
        from datetime import date
        r = _redis()
        k = QUOTA_KEY.format(acc=acc_id, day=date.today().isoformat())
        if int(r.get(k) or 0) > 0:
            r.decr(k)
        if channel:
            r.delete(CH_COOLDOWN_KEY.format(acc=acc_id, ch=channel.lstrip("@")))
    except Exception:
        pass


def _quota_use(acc_id: int, n: int = 1):
    try:
        from datetime import date
        r = _redis()
        k = QUOTA_KEY.format(acc=acc_id, day=date.today().isoformat())
        r.incrby(k, n)
        r.expire(k, 172800)
    except Exception:
        pass


def _strip_html(s: str) -> str:
    s = re.sub(r"<br\s*/?>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"[ \t]+", " ", s).strip()


def _age_sec(posted_at) -> float:
    """Сколько секунд посту. Время берём с самой страницы t.me.

    Если разметка вдруг без времени — считаем пост старым, а не свежим:
    ошибиться в эту сторону дешевле, чем отдать ему очередь впереди
    поста, чей возраст мы знаем точно.
    """
    if not posted_at:
        return 10 ** 6
    try:
        d = datetime.fromisoformat(str(posted_at))
        if d.tzinfo is not None:
            d = d.astimezone(timezone.utc).replace(tzinfo=None)
        return max(0.0, (datetime.utcnow() - d).total_seconds())
    except Exception:
        return 10 ** 6


async def _fetch_channel(session, host, ch, proxy_url):
    """Последний пост канала с публичной страницы.

    Возвращает (номер, время, текст) либо None. Ни аккаунтов, ни их
    лимитов это не касается — обычный HTTP к статической странице.
    """
    import aiohttp
    url = "https://%s/s/%s" % (host, ch.lstrip("@"))
    try:
        # Потолок жёсткий: зависший прокси иначе держит слот семафора
        # двадцать пять секунд и тормозит весь обход.
        async with session.get(url, proxy=proxy_url,
                               timeout=aiohttp.ClientTimeout(
                                   total=FETCH_TIMEOUT_SEC)) as r:
            if r.status != 200:
                return None
            html = await r.text()
    except Exception:
        return None

    ids = RE_POST.findall(html)
    if not ids:
        return None
    post_id = int(ids[-1])
    times = RE_TIME.findall(html)
    posted_at = times[-1] if times else None
    texts = RE_TEXT.findall(html)
    text = _strip_html(texts[-1]) if texts else ""
    return post_id, posted_at, text


async def _proxy_urls(db, limit=None):
    """Несколько живых прокси для обхода — чтобы не бить с одного адреса.

    Сколько именно — зависит от размера наблюдения. На семидесяти каналах
    хватало шести адресов, на семнадцати сотнях с тех же шести пришлось бы
    по три сотни запросов в минуту на каждый.
    """
    if limit is None:
        limit = FETCH_PROXIES
    if not USE_PROXY:
        return [None]
    from sqlalchemy import text as sa_text
    rows = (await db.execute(sa_text("""
        SELECT host, port, login, password FROM proxies
        WHERE is_valid ORDER BY random() LIMIT :lim"""), {"lim": limit})).all()
    out = []
    for h, p, lg, pw in rows:
        if lg and pw:
            out.append("http://%s:%s@%s:%s" % (lg, pw, h, p))
        else:
            out.append("http://%s:%s" % (h, p))
    return out or [None]


async def _pick_account(db, campaign, channel: str, in_flight=None):
    """Кто напишет. Возвращает (id, причина-отказа, кто-занят-очередью).

    Третьим значением отдаём участников, которых не пустило ТОЛЬКО уже
    висящее задание. Для них вызывающий может не отказываться от поста, а
    переписать их ожидающее задание на этот, более свежий — см. вытеснение
    в _watch_once.

    in_flight — сколько заданий у каждого аккаунта уже висит в очереди.
    Больше одного набирать бессмысленно: адрес после сессии остывает
    десять минут, значит аккаунт физически делает не больше шести
    комментов в час. Задания сверх этого простоят и протухнут, а норму
    съедят — ровно это и случилось 08.09: набрали 50, написали 7.

    Пишет ТОЛЬКО тот, кто уже состоит в обсуждении канала. Не «по
    возможности», а обязательно: вступление на лету стоит резолва, лишних
    секунд и заметно на стороне Telegram — аккаунт, который вступил и
    через минуту написал, выглядит хуже, чем давний участник. Если своего
    участника нет, пост просто пропускаем.

    Остальные условия: активен, не отдыхает, не под флудом, не выбрал
    суточную норму и не писал недавно в этот же канал.

    Занятость прокси здесь НЕ проверяем: адрес мог освободиться к моменту
    исполнения, а отбраковать из-за него — значит потерять пост. Очередь
    сама подождёт, этим занимается диспетчер.
    """
    from sqlalchemy import text as sa_text
    from utils.account_health import is_resting, flood_remaining

    ids = [int(x) for x in (campaign.account_ids or [])]
    if not ids:
        return None, "в кампании нет аккаунтов", []

    rows = (await db.execute(sa_text("""
        SELECT a.id FROM accounts a
        JOIN proxies p ON p.id = a.proxy_id
        WHERE a.id = ANY(:ids) AND a.status = 'active' AND p.is_valid
    """), {"ids": ids})).all()
    if not rows:
        return None, "нет живых аккаунтов", []

    ch = channel.lstrip("@")
    r = _redis()

    # Кто состоит в обсуждении. Два источника, оба подтверждают факт, а
    # не намерение: флаг ставится после успешного вступления или коммента,
    # запись в assignments — после подтверждённого join.
    member = set()
    for (acc_id,) in rows:
        if r.exists("gramgpt:disc_joined:%d:%s" % (acc_id, ch)):
            member.add(acc_id)
    if not member:
        joined_rows = (await db.execute(sa_text("""
            SELECT account_id FROM campaign_channel_assignments
            WHERE channel_username = :ch AND status = 'joined'
              AND account_id = ANY(:ids)"""),
            {"ch": ch, "ids": [x[0] for x in rows]})).all()
        member = {x[0] for x in joined_rows}
    if not member:
        return None, "никто не вступил в обсуждение", []

    free = []
    waiting = []
    why = "все участники заняты"
    for acc_id in member:
        if flood_remaining(acc_id) > 0:
            why = "участники под флудом"
            continue
        if is_resting(acc_id)[0]:
            why = "участники отдыхают"
            continue
        if _quota_left(acc_id) <= 0:
            why = "участники выбрали суточную норму"
            continue
        # Два коммента подряд в один канал с одного аккаунта — заметно.
        if r.exists(CH_COOLDOWN_KEY.format(acc=acc_id, ch=ch)):
            why = "участник недавно писал в этот канал"
            continue
        # Уже стоит в очереди — второе задание он всё равно не успеет.
        # Но отказ тут не окончательный: старое задание можно переписать
        # на этот пост, если тот свежее. Возвращаем такого участника
        # отдельным списком.
        if in_flight and in_flight.get(acc_id, 0) >= MAX_IN_FLIGHT:
            why = "у участника уже есть задание в очереди"
            waiting.append(acc_id)
            continue
        free.append(acc_id)

    if not free:
        return None, why, waiting
    return random.choice(free), "", waiting


async def _supersede(db, camp, waiting, channel, post_id, text, new_age, pend):
    """Переписать ожидающее задание на более свежий пост.

    Возвращает id аккаунта, чьё задание переписали, либо None.

    Смысл. Слот аккаунта выстрелит не раньше, чем освободится его адрес,
    а это до десяти минут. Всё это время задание просто лежит, и лежать
    ему лучше со свежим постом: писать под вышедший минуту назад имеет
    смысл, под десятиминутный — почти нет.

    Меняем строку на месте, а не отменяем и создаём новую: норма за этот
    слот уже списана, и лишние отмены только замусорили бы статистику,
    где 'aborted' означает настоящую потерю.

    Порог REPLACE_MARGIN_SEC. Без него мы бы переписывали задание на
    каждом обходе, стоило выйти любому посту, и в статистике вместо
    работы была бы бесконечная перекладка. Меняем только когда ожидающий
    пост ощутимо старее нового.
    """
    from sqlalchemy import text as sa_text

    now = datetime.utcnow()
    r = _redis()
    best, best_stale = None, 0.0
    for acc_id in waiting:
        row = pend.get(acc_id)
        if not row:
            continue
        qid, old_ch, sched = row
        if old_ch.lstrip("@") == channel.lstrip("@"):
            continue                      # тот же канал — менять нечего
        if r.exists(SENT_KEY.format(qid=qid)):
            continue                      # уже у исполнителя, не трогаем
        stale = (now - sched).total_seconds()
        if stale > best_stale:
            best, best_stale = acc_id, stale

    if best is None or best_stale < new_age + REPLACE_MARGIN_SEC:
        return None

    qid, old_ch, _sched = pend[best]
    delay = random.randint(DELAY_MIN, DELAY_MAX)
    new_sched = now + timedelta(seconds=delay)
    await db.execute(sa_text("""
        UPDATE comment_queue
        SET channel = :ch, post_id = :pid, post_text = :txt,
            scheduled_at = :sch
        WHERE id = :id AND status = 'scheduled'"""),
        {"ch": channel, "pid": post_id, "txt": text[:2000],
         "sch": new_sched, "id": qid})

    # Под старый канал мы так и не написали — остывание с него снимаем,
    # иначе аккаунт зря не сможет туда прийти следующие три часа.
    r.delete(CH_COOLDOWN_KEY.format(acc=best, ch=old_ch.lstrip("@")))
    r.setex(CH_COOLDOWN_KEY.format(acc=best, ch=channel.lstrip("@")),
            CH_COOLDOWN_SEC, "1")
    pend[best] = (qid, channel, new_sched)

    logger.info("[realtime] acc#%d: @%s (ждал %dс) → @%s пост %d (возраст %dс)",
                best, old_ch, int(best_stale), channel, post_id, int(new_age))
    return best


async def _watch_once():
    """Один обход всех realtime-кампаний."""
    if API_DIR not in sys.path:
        sys.path.insert(0, API_DIR)

    import aiohttp
    from sqlalchemy import select, text as sa_text
    from models.campaign import Campaign
    from models.comment_queue import CommentQueue
    from utils.db_pool import async_session as Session

    stats = {"channels": 0, "new_posts": 0, "queued": 0,
             "no_account": 0, "seeded": 0, "campaigns": 0}

    async with Session() as db:
        camps = (await db.execute(select(Campaign).where(
            Campaign.campaign_type == "realtime",
            Campaign.status == "active"))).scalars().all()
        if not camps:
            return stats
        stats["campaigns"] = len(camps)
        proxies = await _proxy_urls(db)

        for camp in camps:
            rows = (await db.execute(sa_text("""
                SELECT id, username, last_post_id FROM target_channels
                WHERE campaign_id = :c AND is_active AND mode = 'comment'
            """), {"c": camp.id})).all()
            if not rows:
                continue
            stats["channels"] += len(rows)

            # Сколько заданий уже ждёт у каждого аккаунта.
            in_flight = {x[0]: x[1] for x in (await db.execute(sa_text("""
                SELECT account_id, count(*) FROM comment_queue
                WHERE campaign_id = :c AND status IN ('scheduled', 'executing')
                GROUP BY 1"""), {"c": camp.id})).all()}
            pending = sum(in_flight.values())

            # Сами ожидающие задания — по одному на аккаунт, самое старое.
            # Нужны для вытеснения: чтобы переписать задание на свежий
            # пост, надо знать, какое именно и сколько оно уже ждёт.
            pend = {}
            for qid, aid, qch, qsch in (await db.execute(sa_text("""
                SELECT DISTINCT ON (account_id) id, account_id, channel,
                       scheduled_at
                FROM comment_queue
                WHERE campaign_id = :c AND status = 'scheduled'
                ORDER BY account_id, scheduled_at ASC"""),
                {"c": camp.id})).all():
                pend[aid] = (qid, qch, qsch)

            # Потолок очереди. Считаем не «сколько хочется», а сколько
            # физически успеем отправить, пока задания не протухли: адрес
            # после сессии остывает RT_IP_COOLDOWN_SEC, значит за окно
            # STALE_MIN он даёт столько-то подключений. Всё, что сверх
            # этого, гарантированно уйдёт в мусор.
            n_hosts = (await db.execute(sa_text("""
                SELECT count(DISTINCT p.host) FROM accounts a
                JOIN proxies p ON p.id = a.proxy_id
                WHERE a.id = ANY(:ids)"""),
                {"ids": list(camp.account_ids or [])})).scalar() or 1
            cap_queue = max(2, int(n_hosts * max(
                1.0, STALE_MIN * 60.0 / max(60, RT_IP_COOLDOWN_SEC))))

            sem = asyncio.Semaphore(FETCH_CONCURRENCY)

            # По одному соединению на адрес, а не на канал. Раньше каждый
            # канал заводил свою ClientSession, то есть свой TCP и TLS
            # через прокси: на 73 канала это было незаметно, а на 1752
            # обход занял 96 секунд при интервале в 60 и не успевал.
            # С общей сессией рукопожатия делаются один раз, дальше идёт
            # переиспользование соединений.
            коннектор = aiohttp.TCPConnector(
                limit=FETCH_CONCURRENCY, limit_per_host=FETCH_CONCURRENCY,
                ttl_dns_cache=300, force_close=False)
            таймаут = aiohttp.ClientTimeout(total=FETCH_TIMEOUT_SEC)
            сессия = aiohttp.ClientSession(
                headers={"User-Agent": UA}, connector=коннектор,
                timeout=таймаут)

            async def one(row, idx):
                async with sem:
                    return row, await _fetch_channel(
                        сессия, "t.me", row[1], proxies[idx % len(proxies)])

            начало = datetime.utcnow()
            try:
                got = await asyncio.gather(
                    *[one(r, i) for i, r in enumerate(rows)],
                    return_exceptions=True)
            finally:
                await сессия.close()
            обход_сек = (datetime.utcnow() - начало).total_seconds()
            stats["обход_сек"] = round(обход_сек, 1)

            # ── Проход первый: что вообще вышло нового ──────────────
            # Раньше кандидаты разбирались тут же, в порядке обхода
            # каналов, и первые в списке забирали аккаунты независимо от
            # того, насколько свежий у них пост. При 145 каналах на пять
            # аккаунтов это значило: пишем в тот пост, чей канал раньше
            # стоит в базе, а вышедший минуту назад ждёт и протухает.
            # Поэтому сначала собираем всех, а решаем — потом.
            cand = []
            for item in got:
                if isinstance(item, Exception) or not item:
                    continue
                row, res = item
                if not res:
                    continue
                ch_id, uname, last_seen = row
                post_id, posted_at, text = res

                # Первое знакомство: только запоминаем, не комментируем.
                # Иначе при добавлении канала мы бы написали под старый
                # пост, который давно всем показали.
                if not last_seen:
                    await db.execute(sa_text(
                        "UPDATE target_channels SET last_post_id = :p WHERE id = :i"),
                        {"p": post_id, "i": ch_id})
                    stats["seeded"] += 1
                    continue

                if post_id <= last_seen:
                    continue

                stats["new_posts"] += 1
                await db.execute(sa_text(
                    "UPDATE target_channels SET last_post_id = :p WHERE id = :i"),
                    {"p": post_id, "i": ch_id})

                # Пост без текста комментировать нечем: LLM не из чего
                # строить ответ. Такие в плановом движке регулярно
                # оборачивались отказом no_text_posts.
                if len(text) < 40:
                    continue

                cand.append((_age_sec(posted_at), uname, post_id, text))

            # ── Проход второй: раздаём аккаунты свежайшим ───────────
            # Сортировка по возрасту поста. Пропускная способность у нас
            # меньше, чем поток постов, поэтому вопрос не «кого писать»,
            # а «кого писать первым» — и первым идёт тот, под кем ещё
            # никто не успел отметиться.
            cand.sort()
            for _age, uname, post_id, text in cand:
                r = _redis()
                seen = SEEN_KEY.format(camp=camp.id, ch=uname, post=post_id)
                if r.exists(seen):
                    continue

                # Очередь длиннее пропускной способности бессмысленна:
                # всё сверх неё протухнет, зря заняв суточную норму и
                # остывание канала. Лучше пропустить пост сейчас — через
                # минуту выйдет другой, и он будет свежее этого.
                if pending >= cap_queue:
                    stats["очередь_полна"] = stats.get("очередь_полна", 0) + 1
                    continue

                acc_id, why, waiting = await _pick_account(
                    db, camp, uname, in_flight)

                # ── Вытеснение: свежий пост занимает место старого ───
                # Аккаунт держит одно задание за раз, и раньше при занятом
                # слоте новый пост просто терялся. Но слот всё равно
                # выстрелит только тогда, когда освободится адрес, — и в
                # этот момент выгоднее иметь под рукой пост, вышедший
                # минуту назад, а не десять. Поэтому не отказываемся, а
                # переписываем ожидающее задание на новый пост.
                #
                # Строка при этом одна и та же: норма уже списана за этот
                # слот, лишних записей и возвратов не появляется.
                if not acc_id and waiting:
                    acc_id = await _supersede(
                        db, camp, waiting, uname, post_id, text, _age, pend)
                    if acc_id:
                        stats["вытеснено"] = stats.get("вытеснено", 0) + 1
                        _redis().setex(seen, 86400, "1")
                        continue

                if not acc_id:
                    stats["no_account"] += 1
                    stats.setdefault("причины", {})
                    stats["причины"][why] = stats["причины"].get(why, 0) + 1
                    logger.info("[realtime] @%s пост %d пропущен: %s",
                                uname, post_id, why)
                    continue

                delay = random.randint(DELAY_MIN, DELAY_MAX)
                db.add(CommentQueue(
                    campaign_id=camp.id,
                    account_id=acc_id,
                    channel=uname,
                    post_id=post_id,
                    post_text=text[:2000],
                    personality={}, style={},
                    status="scheduled",
                    scheduled_at=datetime.utcnow() + timedelta(seconds=delay),
                ))
                # Норму и остывание канала занимаем СРАЗУ, при постановке
                # в очередь. Иначе за те минуты, что задание ждёт своей
                # очереди, тот же аккаунт успеет получить ещё несколько
                # заданий и выйдет за суточный потолок.
                _quota_use(acc_id, 1)
                in_flight[acc_id] = in_flight.get(acc_id, 0) + 1
                pending += 1
                r.setex(CH_COOLDOWN_KEY.format(acc=acc_id, ch=uname),
                        CH_COOLDOWN_SEC, "1")
                r.setex(seen, 86400, "1")
                stats["queued"] += 1
                logger.info("[realtime] @%s пост %d → acc#%d через %dс",
                            uname, post_id, acc_id, delay)

        await db.commit()

    try:
        import json
        _redis().setex(STATE_KEY, 3600, json.dumps(
            {**stats, "at": datetime.utcnow().isoformat()}, ensure_ascii=False))
    except Exception:
        pass
    return stats


async def _dispatch_once():
    """Готовые задания — на исполнение, без размазывания по часу.

    Старый dispatch_comments специально растягивает отправку на окно до
    часа, чтобы полсотни комментов не ушли залпом. Здесь это вредно:
    задержка перед комментом уже заложена в scheduled_at, а смысл всей
    затеи — успеть в первые минуты.

    Защита от залпа другая: аккаунт с занятым прокси просто ждёт
    следующего тика, и одновременно работают ровно столько сессий,
    сколько у нас свободных адресов.
    """
    if API_DIR not in sys.path:
        sys.path.insert(0, API_DIR)

    from sqlalchemy import select, text as sa_text
    from models.comment_queue import CommentQueue
    from models.campaign import Campaign
    from utils.db_pool import async_session as Session
    from utils.ip_throttle import is_ip_busy

    sent = 0
    busy = 0
    async with Session() as db:
        rt_ids = [x[0] for x in (await db.execute(sa_text(
            "SELECT id FROM campaigns WHERE campaign_type = 'realtime'"))).all()]
        if not rt_ids:
            return {"dispatched": 0}

        now = datetime.utcnow()

        # ── Сперва уборка, потом раздача ────────────────────────────
        # Раньше протухшее отбраковывалось прямо в цикле раздачи, то есть
        # только то, что попало в окно выборки. Пока разбор шёл от старых
        # к новым, это работало: старое и лежало в начале. Теперь порядок
        # обратный, и без отдельной уборки хвост очереди не попадал бы в
        # окно уже никогда — висел бы в 'scheduled' вечно, держа за собой
        # суточную норму аккаунта и остывание канала.
        dead = (await db.execute(select(CommentQueue).where(
            CommentQueue.status == "scheduled",
            CommentQueue.campaign_id.in_(rt_ids),
            CommentQueue.scheduled_at < now - timedelta(minutes=STALE_MIN),
        ))).scalars().all()
        stale = 0
        for it in dead:
            it.status = "aborted"
            it.error = "протухло: ждало дольше %d мин" % STALE_MIN
            it.executed_at = now
            # Коммента не было — норму возвращаем, канал отпускаем.
            _quota_give_back(it.account_id, it.channel)
            stale += 1

        # ── Свежие вперёд ───────────────────────────────────────────
        # Каналов у кампании кратно больше, чем мы успеваем обслужить,
        # поэтому очередь почти всегда длиннее пропускной способности.
        # Разбирать её с головы — значит всегда писать в самый старый из
        # найденных постов: пока до него доходит очередь, под ним уже
        # набежали комментарии, а весь смысл затеи был успеть первым.
        # Свежие при этом ждали бы своего часа и протухали.
        # Берём наоборот, от новых к старым: скудную пропускную
        # способность тратим на те посты, где она ещё что-то решает.
        items = (await db.execute(select(CommentQueue).where(
            CommentQueue.status == "scheduled",
            CommentQueue.campaign_id.in_(rt_ids),
            CommentQueue.scheduled_at <= now,
        ).order_by(CommentQueue.scheduled_at.desc()).limit(60))).scalars().all()

        hosts = dict((x[0], x[1]) for x in (await db.execute(sa_text("""
            SELECT a.id, p.host FROM accounts a JOIN proxies p ON p.id = a.proxy_id
        """))).all())

        taken = set()
        for it in items:
            host = hosts.get(it.account_id)
            # Один адрес — одна сессия за тик. Здесь только СМОТРИМ:
            # захватывает адрес сам исполнитель, когда подключается.
            # Спрашиваем от имени аккаунта: адрес, закреплённый за ним,
            # свободен для него и занят для остальных.
            if host and (host in taken
                         or is_ip_busy(host, owner=it.account_id)):
                busy += 1
                continue
            if host:
                taken.add(host)
            # Метку ставим ДО отправки: между send_task и началом работы
            # исполнителя наблюдатель успел бы вклиниться.
            _redis().setex(SENT_KEY.format(qid=it.id), 3600, "1")
            celery_app.send_task(
                "tasks.comment_executor.execute_single_comment",
                args=[it.id], queue="ai_dialogs")
            sent += 1

        if stale:
            await db.commit()

    if sent or busy or stale:
        logger.info("[realtime] отправлено %d, ждут свободный адрес %d, "
                    "протухло %d", sent, busy, stale)
    return {"dispatched": sent, "postponed": busy, "stale": stale}


@celery_app.task(bind=True, name="tasks.realtime_comments.watch_channels")
def watch_channels(self):
    """Обход каналов realtime-кампаний: ищем новые посты."""
    return run_async(_watch_once())


@celery_app.task(bind=True, name="tasks.realtime_comments.dispatch_realtime")
def dispatch_realtime(self):
    """Отправка готовых заданий realtime-очереди на исполнение."""
    return run_async(_dispatch_once())
