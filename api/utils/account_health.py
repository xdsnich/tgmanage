"""
utils/account_health.py — «отдых» аккаунта по РЕАЛЬНЫМ претензиям Telegram.

Главное требование (юзер, 2026-08-22): аккаунт должен уходить отдыхать
только когда его отчитал сам Telegram. Не когда LLM не ответила, не когда
у канала закрыты комменты, не когда упал прокси. Поэтому единственный вход
сюда — `record_error()`, который сначала спрашивает у error_origin, кто
вообще сказал эту ошибку, и реагирует ТОЛЬКО на severity='account'.

Почему пауза, а не freeze:
  Раньше PEER_FLOOD и AUTH_KEY_UNREGISTERED одинаково ставили
  acc.status='frozen', то есть списывали аккаунт навсегда. Но PEER_FLOOD —
  состояние обратимое: Telegram включил анти-спам, через сутки-двое
  отпускает. Списывать за него живой аккаунт расточительно. Теперь:
    PEER_FLOOD / крупный FLOOD_WAIT → пауза в Redis (аккаунт жив)
    AUTH_KEY_UNREGISTERED / FROZEN  → это правда конец, статус в БД

Всё состояние в Redis с TTL — само рассасывается, отдельная чистка не нужна.
Redis здесь древний (3.0.504), поэтому только базовые команды:
INCR / EXPIRE / SET ex / GET / TTL / DEL.
"""

import os
import hashlib
import logging
from datetime import date

from utils.redis_pool import get_redis as _get_redis
from utils.error_origin import classify_severity, parse_flood_wait

logger = logging.getLogger(__name__)

_REST_KEY = "gramgpt:acc_rest:{acc_id}"           # значение = причина
_FLOOD_CNT_KEY = "gramgpt:acc_flood:{acc_id}:{day}"
_ACC_ERR_KEY = "gramgpt:acc_err:{acc_id}:{day}"   # все account-severity за день
# Дедупликация: одна и та же ошибка, прилетевшая из нескольких мест.
_DEDUP_KEY = "gramgpt:health_dedup:{acc_id}:{sig}"
HEALTH_DEDUP_SEC = int(os.getenv("HEALTH_DEDUP_SEC", "30"))

# Разные каналы, где аккаунту персонально отказали в записи (см. ниже).
_PERSONAL_KEY = "gramgpt:acc_personal_refusal:{acc_id}"
# Отдельная отметка «Telegram сейчас держит аккаунт под флудом».
# Зачем отдельно от _REST_KEY: set_rest не укорачивает более длинный отдых,
# а у карантинных аккаунтов уже висит многосуточный отдых с причиной
# «@SpamBot: аккаунт ограничен». Флуд на пять часов в такой ключ просто не
# записывался, и код, который считает спамботовский отдых несущественным для
# чтения, продолжал ходить в Telegram под флудом. 27.08 так пострадали 16
# аккаунтов из 28 работавших.
_FLOOD_UNTIL_KEY = "gramgpt:acc_flood_until:{acc_id}"
_RESTRICTED_KEY = "gramgpt:acc_restricted:{acc_id}"

# ── Персональные отказы: когда виноват аккаунт, а не канал ──────────
# `user_banned` и `chat_write_forbidden` Telegram отдаёт в двух разных
# ситуациях: аккаунт забанен именно в этом канале (тогда виноват канал),
# либо аккаунт ограничен глобально и ему нигде не дают писать. По одному
# отказу их не различить — поэтому severity остаётся 'channel'.
# Но если отказы копятся в РАЗНЫХ каналах, вопрос уже к аккаунту.
#
# Замер 2026-08-25 (кампания #139): acc#55 собрал 15 отказов user_banned
# в 13 разных каналах и не написал НИ ОДНОГО коммента за всё время;
# @SpamBot подтвердил глобальное ограничение. Ещё 4 аккаунта с такой же
# картиной проверены тем же способом — ограничены все.
PERSONAL_REFUSAL_MARKERS = (
    "USER_BANNED", "USERBANNEDINCHANNEL",
    "CHAT_WRITE_FORBIDDEN", "CHATWRITEFORBIDDEN",
)
# Сколько РАЗНЫХ каналов должно отказать, чтобы обвинить аккаунт.
# Порог поднят с 3 до 5, а отдых укорочен с 48 часов до двух.
# Причина — счёт правила по проверкам через @SpamBot: 8 ложных
# срабатываний против 2 настоящих, и оба настоящих оказались временными
# ограничениями, которые сами отпустили за сутки. Каждое ложное стоило
# двух суток простоя, а 02.09 правило разом заперло семь аккаунтов.
# `user_banned` приходит и когда аккаунт забанен именно в том канале, а
# три разных канала за неделю набираются сами собой у тех, кто пишет
# больше всех.
PERSONAL_CH_LIMIT = int(os.getenv("HEALTH_PERSONAL_CH_LIMIT", "5"))
# Отметка «аккаунт недавно успешно писал». Ставит plan_executor после
# каждого отправленного коммента. Ограниченный аккаунт писать не может
# нигде — значит наличие отметки опровергает подозрение.
_WROTE_KEY = "gramgpt:acc_wrote:{acc_id}"
WROTE_TTL = int(os.getenv("HEALTH_WROTE_TTL", "21600"))     # 6 часов


def mark_wrote(account_id: int):
    """Запомнить, что аккаунт только что успешно отправил коммент."""
    try:
        _get_redis().set(_WROTE_KEY.format(acc_id=account_id), "1", ex=WROTE_TTL)
    except Exception:
        pass


def wrote_recently(account_id: int) -> bool:
    try:
        return bool(_get_redis().get(_WROTE_KEY.format(acc_id=account_id)))
    except Exception:
        return False
PERSONAL_WINDOW_SEC = int(os.getenv("HEALTH_PERSONAL_WINDOW_SEC", str(7 * 86400)))
# Отдых при обвинении. Не «навсегда»: ограничение снимается само,
# аккаунт должен вернуться в работу, а не быть списанным.
PERSONAL_REST_SEC = int(os.getenv("HEALTH_PERSONAL_REST_SEC", str(2 * 3600)))

# ── Пороги ──────────────────────────────────────────────────
# Мелкий FLOOD_WAIT — норма, Telegram так тормозит любого живого юзера.
# Отдых нужен когда он крупный или когда мелких много подряд.
FLOOD_BIG_SEC = int(os.getenv("HEALTH_FLOOD_BIG_SEC", "300"))       # >5 мин = серьёзно
FLOOD_PER_DAY_LIMIT = int(os.getenv("HEALTH_FLOOD_PER_DAY", "3"))   # 3 мелких за день
PEER_FLOOD_REST_SEC = int(os.getenv("HEALTH_PEER_FLOOD_REST_SEC", str(36 * 3600)))
FLOOD_DAY_REST_SEC = int(os.getenv("HEALTH_FLOOD_DAY_REST_SEC", str(24 * 3600)))
FLOOD_MARGIN_SEC = 600      # запас поверх запрошенного Telegram ожидания


def _today() -> str:
    return date.today().isoformat()


# ── Чтение состояния ────────────────────────────────────────

def is_resting(account_id: int) -> tuple[bool, str]:
    """(отдыхает?, причина). Fail-open: Redis лёг → работаем."""
    try:
        v = _get_redis().get(_REST_KEY.format(acc_id=account_id))
        if not v:
            return False, ""
        return True, (v.decode() if isinstance(v, bytes) else str(v))
    except Exception:
        return False, ""


def rest_remaining(account_id: int) -> int:
    """Сколько секунд отдыха осталось (0 если не отдыхает)."""
    try:
        ttl = _get_redis().ttl(_REST_KEY.format(acc_id=account_id))
        return int(ttl) if ttl and ttl > 0 else 0
    except Exception:
        return 0


def flood_remaining(account_id: int) -> int:
    """Секунды до конца флуда, 0 если флуда нет.

    Проверять ОБЯЗАТЕЛЬНО всем, кто решает игнорировать «мягкий» отдых:
    под FLOOD_WAIT любой запрос углубляет наказание, тут исключений нет.
    """
    try:
        ttl = _get_redis().ttl(_FLOOD_UNTIL_KEY.format(acc_id=account_id))
        return int(ttl) if ttl and ttl > 0 else 0
    except Exception:
        return 0


def _mark_flood(account_id: int, seconds: int):
    """Ставит отметку о флуде независимо от того, какой отдых уже висит."""
    try:
        _get_redis().set(_FLOOD_UNTIL_KEY.format(acc_id=account_id),
                         str(int(seconds)), ex=int(seconds))
    except Exception:
        pass


def set_rest(account_id: int, seconds: int, reason: str):
    """Отправить аккаунт отдыхать. Более длинный существующий отдых не укорачиваем."""
    try:
        r = _get_redis()
        key = _REST_KEY.format(acc_id=account_id)
        cur = r.ttl(key)
        if cur and cur > seconds:
            return          # уже отдыхает дольше — не сокращаем
        r.set(key, reason[:120], ex=int(seconds))
        logger.warning(
            f"[health] acc#{account_id} → отдых {seconds // 3600}ч {(seconds % 3600) // 60}м: {reason}"
        )
    except Exception as e:
        logger.warning(f"[health] set_rest fail acc#{account_id}: {e}")


def clear_rest(account_id: int):
    try:
        _get_redis().delete(_REST_KEY.format(acc_id=account_id))
    except Exception:
        pass


def get_stats(account_id: int) -> dict:
    """Для UI/диагностики."""
    resting, reason = is_resting(account_id)
    out = {"resting": resting, "reason": reason, "remaining_sec": rest_remaining(account_id)}
    try:
        r = _get_redis()
        out["flood_today"] = int(r.get(_FLOOD_CNT_KEY.format(acc_id=account_id, day=_today())) or 0)
        out["account_errors_today"] = int(r.get(_ACC_ERR_KEY.format(acc_id=account_id, day=_today())) or 0)
    except Exception:
        pass
    return out


# ── Единственный вход для регистрации ошибки ────────────────

def record_error(account_id: int, error_code: str = "",
                 exc: BaseException | None = None,
                 channel: str | None = None) -> dict:
    """Регистрирует ошибку и, если это претензия Telegram к аккаунту,
    отправляет его отдыхать.

    Возвращает {'severity', 'action', 'rest_sec'} — вызывающий код может
    решить, прерывать ли сессию.

    Ошибки от нас самих (llm_empty, no_posts) и проблемы каналов
    (chat_write_forbidden) сюда попадают, но НИЧЕГО не делают —
    severity вернётся 'none' или 'channel'.
    """
    sev = classify_severity(error_code, exc)
    result = {"severity": sev, "action": "none", "rest_sec": 0}

    # ── Персональный отказ в разных каналах → виноват аккаунт ──
    if sev == "channel" and channel:
        _blob = (error_code or "")
        if exc is not None:
            _blob += " " + type(exc).__name__ + " " + str(exc)
        if any(m in _blob.upper() for m in PERSONAL_REFUSAL_MARKERS):
            try:
                r = _get_redis()
                k = _PERSONAL_KEY.format(acc_id=account_id)
                r.sadd(k, channel.lstrip("@").lower())
                r.expire(k, PERSONAL_WINDOW_SEC)
                n_ch = int(r.scard(k) or 0)
                if n_ch >= PERSONAL_CH_LIMIT and wrote_recently(account_id):
                    # Писал за последние шесть часов — значит писать может,
                    # и глобального ограничения нет. Отказы в этих каналах
                    # персональные, к аккаунту в целом отношения не имеют.
                    logger.info(
                        f"[health] acc#{account_id}: отказ в {n_ch} каналах, но "
                        f"коммент недавно ушёл — не ограничен, не трогаем"
                    )
                    return {"severity": "channel", "action": "none", "rest_sec": 0}
                if n_ch >= PERSONAL_CH_LIMIT:
                    r.setex(_RESTRICTED_KEY.format(acc_id=account_id),
                            PERSONAL_WINDOW_SEC, str(n_ch))
                    reason = (f"персональный отказ в {n_ch} разных каналах "
                              f"— похоже на глобальное ограничение аккаунта")
                    set_rest(account_id, PERSONAL_REST_SEC, reason)
                    logger.error(f"[health] acc#{account_id}: {reason} → отдых 48ч, "
                                 f"проверьте через @SpamBot")
                    return {"severity": "account", "action": "rest",
                            "rest_sec": PERSONAL_REST_SEC, "restricted_channels": n_ch}
            except Exception:
                pass

    if sev != "account":
        return result

    blob = (error_code or "")
    if exc is not None:
        blob += " " + type(exc).__name__ + " " + str(exc)
    up = blob.upper()

    # ── Дедупликация ──────────────────────────────────────
    # record_error теперь зовётся из нескольких мест сразу: перехватчик
    # на клиенте ловит ошибку первым, следом её же логирует _safe_log,
    # а кое-где есть и явный вызов. Один физический FLOOD_WAIT не должен
    # трижды крутить суточный счётчик — иначе накопительный отдых
    # (FLOOD_PER_DAY_LIMIT) сработает на ровном месте.
    # Окно 30с: дубли прилетают в пределах одной миллисекунды, а
    # настоящие повторы приходят заметно позже.
    try:
        r = _get_redis()
        # Подпись строим по СУТИ ошибки, а не по тексту вызывающего. Одно и
        # то же событие приходит из перехватчика клиента как «FloodWaitError»,
        # а из явного вызова в пробе — как «FloodWait 900». Строки разные,
        # хэши разные, дедупликация промахивалась, и суточный счётчик флудов
        # рос вдвое: 27.08 он показал 46 отметок на 23 настоящих флуда.
        if exc is not None:
            _sig_src = f"{type(exc).__name__}:{parse_flood_wait(error_code, exc)}"
        else:
            _sig_src = up[:200]
        _sig = hashlib.md5(_sig_src.encode("utf-8", "replace")).hexdigest()[:12]
        if not r.set(_DEDUP_KEY.format(acc_id=account_id, sig=_sig), "1",
                     nx=True, ex=HEALTH_DEDUP_SEC):
            # уже посчитали эту же ошибку — возвращаем текущее состояние,
            # не трогая счётчики
            resting, why = is_resting(account_id)
            result.update(action="rest" if resting else "none", deduped=True)
            return result
    except Exception:
        pass

    try:
        r = _get_redis()
        # общий счётчик account-претензий за день (для UI)
        k_err = _ACC_ERR_KEY.format(acc_id=account_id, day=_today())
        r.incr(k_err)
        r.expire(k_err, 86400)
    except Exception:
        pass

    # ── Фатальное: аккаунт действительно потерян ──────────
    if any(m in up for m in ("AUTH_KEY_UNREGISTERED", "USER_DEACTIVATED", "USERDEACTIVATED",
                             "PHONE_NUMBER_BANNED", "SESSION_REVOKED")):
        result["action"] = "dead"
        return result
    if "FROZEN_METHOD_INVALID" in up or "NOT AVAILABLE FOR FROZEN ACCOUNTS" in up:
        result["action"] = "frozen"
        return result

    # ── PEER_FLOOD: анти-спам включён. Обратимо → длинная пауза ──
    if "PEER_FLOOD" in up or "PEERFLOOD" in up:
        _mark_flood(account_id, PEER_FLOOD_REST_SEC)
        set_rest(account_id, PEER_FLOOD_REST_SEC, "PEER_FLOOD (анти-спам Telegram)")
        result.update(action="rest", rest_sec=PEER_FLOOD_REST_SEC)
        return result

    # ── FLOOD_WAIT ────────────────────────────────────────
    wait = parse_flood_wait(error_code, exc)
    if wait > 0 or "FLOOD" in up:
        try:
            r = _get_redis()
            k = _FLOOD_CNT_KEY.format(acc_id=account_id, day=_today())
            cnt = int(r.incr(k))
            r.expire(k, 86400)
        except Exception:
            cnt = 1

        if wait >= FLOOD_BIG_SEC:
            rest = wait + FLOOD_MARGIN_SEC
            _mark_flood(account_id, rest)
            set_rest(account_id, rest, f"FLOOD_WAIT {wait}с")
            result.update(action="rest", rest_sec=rest)
        elif cnt >= FLOOD_PER_DAY_LIMIT:
            _mark_flood(account_id, FLOOD_DAY_REST_SEC)
            set_rest(account_id, FLOOD_DAY_REST_SEC,
                     f"{cnt} FLOOD_WAIT за сутки — накопительный отдых")
            result.update(action="rest", rest_sec=FLOOD_DAY_REST_SEC)
        else:
            # мелкий и первый — просто переждать столько, сколько просят
            _mark_flood(account_id, wait)
            result.update(action="wait", rest_sec=wait)
        return result

    # ── USER_RESTRICTED и прочее account-уровня ───────────
    set_rest(account_id, FLOOD_DAY_REST_SEC, f"Telegram ограничил аккаунт: {blob[:60]}")
    result.update(action="rest", rest_sec=FLOOD_DAY_REST_SEC)
    return result
