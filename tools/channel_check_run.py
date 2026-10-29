# -*- coding: utf-8 -*-
"""Проверка каналов аккаунтами — каждый день, пока каналы не кончатся.

Один проход (bulk_channel_check) выбирает суточную квоту всех аккаунтов:
по 120 каналов на аккаунт, 15-30 в час, адреса параллельно, аккаунты
одного адреса по очереди. Потом ждём новых суток — квота считается по
дате — и идём снова. Флуд аккаунта = он выходит, канал возвращается в
очередь, адрес дальше работает медленнее.

Ничего не удаляется: непригодные получают вердикт и уезжают в unusable_*,
для паутинки они остаются.

Стоп: задача tasks.bulk_channel_check.stop_bulk_check или флаг в Redis
gramgpt:bulkchk:stop — проход доведёт текущий канал и выйдет, а этот
цикл завершится.

Запуск из папки api:
    python ..\\tools\\channel_check_run.py
"""
import asyncio, os, sys
from datetime import datetime, timedelta
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.abspath("."))

# Проверяем рабочими и карантинными: кампания стоит, прокси у сотни.
os.environ.setdefault("BULKCHK_STATUSES", "active,quarantine")
# Сейчас важны каналы от 10к — их тысячи новых. Мелочь потом.
os.environ.setdefault("BULKCHK_MIN_SUBS", "10000")
# Жека и Мария — никогда; #118, #119 (сессия убита AuthKeyDuplicated) и
# #175 (разлогинен) — до повторного входа (проверка аккаунтов 01.10).
os.environ.setdefault("BULKCHK_EXCLUDE", "48,95,217,118,119,175")

ЛОГ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "channel_check_run.log")


def журнал(s):
    строка = "[%s] %s" % (datetime.now().strftime("%d.%m %H:%M:%S"), s)
    print(строка, flush=True)
    with open(ЛОГ, "a", encoding="utf-8") as f:
        f.write(строка + "\n")


ТИШИНА_МИН = int(os.getenv("BULKCHK_WATCHDOG_MIN", "45"))


async def с_сторожем(проход):
    """Запустить проход и следить, чтобы он не завис.

    03.10 один аккаунт повис на запросе без таймаута, и проход простоял
    17 часов — даже следующий, ночной, не начался. Сторож смотрит на
    счётчик проверок: если за ТИШИНА_МИН минут он не сдвинулся, проход
    снимается и цикл идёт дальше. Ложная тревога безвредна: когда квоты
    выбраны, новый проход сразу увидит это и уйдёт ждать следующих суток.
    """
    import json
    from utils.redis_pool import get_redis
    задача = asyncio.ensure_future(проход())
    было, тихо = None, 0
    while not задача.done():
        await asyncio.wait([задача], timeout=300)
        if задача.done():
            break
        try:
            s = json.loads(get_redis().get("gramgpt:bulkchk:progress") or "{}")
        except Exception:
            s = {}
        стало = s.get("checked")
        тихо = тихо + 5 if стало == было else 0
        было = стало
        if тихо >= ТИШИНА_МИН:
            журнал("СТОРОЖ: %d мин ни одной проверки — снимаю проход" % тихо)
            задача.cancel()
            try:
                await задача
            except BaseException:
                pass
            return {"status": "снят сторожем", "checked": стало}
    return задача.result()


async def main():
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        handlers=[logging.FileHandler(ЛОГ.replace(".log", "_detail.log"), encoding="utf-8")])
    from tasks.bulk_channel_check import _run_bulk_check, _stopped
    from tasks.promote_checked import _promote
    журнал("══ старт: статусы %s, от %s подписчиков ══"
           % (os.environ["BULKCHK_STATUSES"], os.environ["BULKCHK_MIN_SUBS"]))
    while True:
        st = await с_сторожем(_run_bulk_check)
        журнал("проход: %s" % {k: st.get(k) for k in
                                ("status", "total", "checked", "remaining", "floods", "errors", "by_reason")})
        try:
            журнал("разбор отстойника: %s" % await _promote())
        except Exception as e:
            журнал("разбор не вышел: %s" % e)
        if st.get("status") == "nothing_to_check" or _stopped():
            журнал("СТОП: %s" % ("каналы кончились" if st.get("status") == "nothing_to_check" else "попросили остановить"))
            return
        # Квота по дате: ждём следующих суток (+случайные минуты).
        завтра = (datetime.now() + timedelta(days=1)).replace(hour=0, minute=15, second=0)
        ждать = max(600, (завтра - datetime.now()).total_seconds())
        журнал("квоты выбраны — жду до %s" % завтра.strftime("%d.%m %H:%M"))
        await asyncio.sleep(ждать)


asyncio.run(main())
