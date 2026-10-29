"""
ufo.gram — tasks/promote_checked.py
Разбор отстойника: проверенные каналы едут по своим папкам.

Зачем. Свежий импорт кладётся в отстойник new_{гео}: в гео-папке должно
лежать только то, куда РЕАЛЬНО можно писать, иначе кампания наберёт целей
с закрытыми обсуждениями и сожжёт слоты на исполнении.

Проверка выносит вердикт и пишет его в channel_capabilities, но сам канал
так и остаётся в отстойнике. Без этого шага получалось глухо: 9303 канала
проверены, 3159 из них пригодны — а в работу не попал ни один, и счётчик
в интерфейсе не двигался неделю.

Что делаем:
  • пригодные  → папка гео (new_ru → ru) и has_comments = true;
  • непригодные → unusable_{гео}, has_comments = false;
  • непроверенные остаются ждать своей очереди.

has_comments трогаем потому, что по нему считается «с комментариями» в
интерфейсе и по нему же кампания отбирает цели. При импорте из TGStat мы
ставим false — тогда вердикта ещё нет и обещать нечего.

Задача идёт по расписанию: проверка работает пачками круглосуточно, и
разбирать её результат надо так же, а не руками раз в неделю.
"""

import asyncio
import os
import sys
import logging

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


async def _promote():
    if API_DIR not in sys.path:
        sys.path.insert(0, API_DIR)

    from sqlalchemy import text as sa_text
    from utils.db_pool import async_session as Session

    async with Session() as db:
        # Пригодные: в гео-папку, с флагом «есть комментарии».
        # substring снимает префикс отстойника: new_ru → ru.
        # Имена — без учёта регистра: вердикт мог записаться как @nobyda,
        # а канал лежит как @NobyDa. 01.10 так застряли в отстойниках 1765
        # проверенных каналов.
        good = await db.execute(sa_text("""
            UPDATE parsed_channels pc
            SET folder = substring(pc.folder from 5),
                has_comments = true
            FROM channel_capabilities cc
            WHERE lower(cc.channel_username) = lower(pc.username)
              AND pc.folder LIKE 'new\\_%'
              AND cc.commentable
        """))

        # Непригодные: в отдельную папку, чтобы не мешались в гео, но и
        # не пропали — вердикт может измениться, да и как семя для
        # паутинки канал годится.
        bad = await db.execute(sa_text("""
            UPDATE parsed_channels pc
            SET folder = 'unusable_' || substring(pc.folder from 5),
                has_comments = false
            FROM channel_capabilities cc
            WHERE lower(cc.channel_username) = lower(pc.username)
              AND pc.folder LIKE 'new\\_%'
              AND NOT cc.commentable
        """))
        await db.commit()

        left = (await db.execute(sa_text("""
            SELECT count(*) FROM parsed_channels
            WHERE folder LIKE 'new\\_%'"""))).scalar()

    res = {"пригодных": good.rowcount or 0,
           "непригодных": bad.rowcount or 0,
           "осталось в отстойнике": left or 0}
    if res["пригодных"] or res["непригодных"]:
        logger.info("[promote] в гео %d, в непригодные %d, ждут проверки %d",
                    res["пригодных"], res["непригодных"],
                    res["осталось в отстойнике"])
    return res


@celery_app.task(bind=True, name="tasks.promote_checked.promote_checked")
def promote_checked(self):
    """Разложить проверенные каналы из отстойника по папкам."""
    return run_async(_promote())
