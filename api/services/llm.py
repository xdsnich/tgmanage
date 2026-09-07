"""
GramGPT — services/llm.py
LLM провайдеры для генерации комментариев.
Используется в commenting_tasks.py и run_listener.py

Поддерживает явную передачу api_key из БД (ServiceCredential).
Если api_key не передан — fallback на env переменные (для обратной совместимости).
"""

import os
import logging
import threading
import time as _time

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

logger = logging.getLogger(__name__)

# ── Rate Limiter ─────────────────────────────────────────────

_llm_lock = threading.Lock()
_llm_calls = []
_last_call_time = 0.0
# ЛИМИТЫ УБРАНЫ 2026-07-23 (по запросу юзера — "убери все лимиты, ебашь 700").
# Если groq/claude вернёт 429 — сам провайдер это сделает, наш код не блокирует.
LLM_MAX_PER_MINUTE = int(os.getenv("LLM_MAX_PER_MINUTE", "9999"))
LLM_MIN_INTERVAL_SEC = float(os.getenv("LLM_MIN_INTERVAL_SEC", "0"))


def _check_rate_limit() -> bool:
    """Rate-limit ОТКЛЮЧЁН (по умолчанию 9999/мин). Оставлен для override через env
    если провайдер начнёт возвращать 429 — тогда снизить через LLM_MAX_PER_MINUTE."""
    global _last_call_time
    with _llm_lock:
        now = _time.time()
        _llm_calls[:] = [t for t in _llm_calls if now - t < 60]
        if len(_llm_calls) >= LLM_MAX_PER_MINUTE:
            logger.warning(f"Rate limit: {len(_llm_calls)}/{LLM_MAX_PER_MINUTE}/мин")
            return False
        if LLM_MIN_INTERVAL_SEC > 0:
            time_since = now - _last_call_time
            if time_since < LLM_MIN_INTERVAL_SEC:
                _time.sleep(LLM_MIN_INTERVAL_SEC - time_since)
        current = _time.time()
        _llm_calls.append(current)
        _last_call_time = current
        return True


# ── Провайдеры ───────────────────────────────────────────────

# ── Коды причин отказа ───────────────────────────────────────
# Раньше ЛЮБОЙ сбой возвращал "" и писался в лог одним словом
# `llm_empty`. Под ним неразличимо смешивались: исчерпанная квота (429),
# снятая с обслуживания модель (404), битый ключ (401), обрыв сети и
# реально пустой ответ модели. Из-за этого июльский инцидент (кончилась
# квота единственного ключа: 23-24.07 → 897 llm_empty) и августовский
# (Groq снял llama-3.3-70b-versatile → 404) выглядели в логах ОДИНАКОВО,
# и причину искали месяц. Теперь причина возвращается явно.
LLM_OK           = "ok"
LLM_RATE_LIMIT   = "llm_rate_limit"     # 429 — упёрлись в квоту
LLM_MODEL_404    = "llm_model_404"      # модель снята с обслуживания
LLM_BAD_KEY      = "llm_bad_key"        # 401 — ключ невалиден
LLM_NETWORK      = "llm_network"        # таймаут / обрыв / 5xx
LLM_NO_KEYS      = "llm_no_keys"        # пул ключей пуст
LLM_TRULY_EMPTY  = "llm_truly_empty"    # модель ответила, но пустой строкой
LLM_LOCAL_LIMIT  = "llm_local_limit"    # наш собственный rate-limiter
LLM_POST_NO_TEXT = "post_no_text"       # у поста нет текста (фото/видео без подписи)
LLM_REFUSAL      = "llm_refusal"        # модель ответила отмазкой вместо коммента

# Ниже скольких символов пост считается «без текста».
# Замер 2026-08-24 по comment_logs: 95 из 1004 комментов (9.5%) были
# написаны к посту с ПУСТЫМ текстом, и 39 из них опубликовали в реальные
# каналы отмазку модели — «No post provided. Please provide the post text».
# Модель физически не могла ничего прокомментировать: ей передавали "".
MIN_POST_CHARS = int(os.getenv("LLM_MIN_POST_CHARS", "10"))

# Признаки того, что модель не прокомментировала, а попросила текст либо
# отказалась. Такой ответ НЕПУСТОЙ, поэтому старая проверка `if not text`
# его пропускала и он уходил в канал как настоящий комментарий.
_REFUSAL_MARKERS = (
    "no post provided", "no post to comment", "no text was provided",
    "there is no post", "didn't provide the post", "did not provide the post",
    "haven't provided the post", "have not provided the post",
    "please provide the post", "please provide the text", "provide the post text",
    "as an ai", "i'm unable to", "i am unable to", "i cannot comment",
    "i can't comment", "i'm sorry, but", "i am sorry, but",
    "не предоставили", "предоставьте текст", "нет текста поста",
    "отсутствует текст", "не могу прокомментировать", "как языковая модель",
)


def looks_like_refusal(text: str) -> bool:
    """True, если модель вместо коммента попросила текст поста / отказалась.

    Проверяем только НАЧАЛО ответа (первые 160 символов): живой коммент
    может содержать «please provide» где-то в середине как часть мысли,
    а отмазка всегда начинается с неё.
    """
    if not text:
        return False
    head = text.strip().lower()[:160]
    return any(m in head for m in _REFUSAL_MARKERS)


def generate_comment(
    provider: str,
    system_prompt: str,
    post_text: str,
    api_key: str = None,
    api_keys: list[str] | None = None,
) -> str:
    """Обратная совместимость: только текст. Причину даёт generate_comment_ex."""
    text, _reason = generate_comment_ex(provider, system_prompt, post_text,
                                        api_key=api_key, api_keys=api_keys)
    return text


def generate_comment_ex(
    provider: str,
    system_prompt: str,
    post_text: str,
    api_key: str = None,
    api_keys: list[str] | None = None,
) -> tuple[str, str]:
    """
    Генерирует комментарий через выбранный LLM.

    Args:
        provider: claude | openai | gemini | groq
        system_prompt: системный промпт
        post_text: текст поста
        api_key: одиночный ключ (backward-compat).
        api_keys: пул ключей для ротации (groq). При 429/пустом ответе
            автоматически переключается на следующий. Если передан —
            приоритетнее api_key.
    """
    # Guard #1 — пост без текста. Раньше сюда прилетало post_text=""
    # (фото/видео без подписи), модель отвечала «дайте текст поста», и эта
    # фраза публиковалась в канал как комментарий. Не тратим ни запрос к
    # LLM, ни попытку отправки: причина `post_no_text` — внутренняя,
    # аккаунт за неё не наказывается.
    if len((post_text or "").strip()) < MIN_POST_CHARS:
        return ("", LLM_POST_NO_TEXT)

    if not _check_rate_limit():
        return ("", LLM_LOCAL_LIMIT)

    text, reason = _dispatch_provider(provider, system_prompt, post_text,
                                      api_key=api_key, api_keys=api_keys)

    # Guard #2 — страховка: модель всё-таки ответила отмазкой.
    # Ответ непустой, поэтому проверка `if not comment_text` у вызывающего
    # его пропустит; ловим здесь, до того как он уйдёт в Telegram.
    if text and looks_like_refusal(text):
        logger.warning(f"LLM отдал отмазку вместо коммента: {text[:80]!r}")
        return ("", LLM_REFUSAL)
    return (text, reason)


def _dispatch_provider(
    provider: str,
    system_prompt: str,
    post_text: str,
    api_key: str = None,
    api_keys: list[str] | None = None,
) -> tuple[str, str]:
    """Собственно вызов провайдера, без guard-ов."""
    if provider == "groq":
        # groq умеет ротацию: собираем pool = api_keys ∪ [api_key]
        pool: list[str] = []
        if api_keys:
            pool.extend(k for k in api_keys if k)
        if api_key and api_key not in pool:
            pool.append(api_key)
        return _call_groq(system_prompt, post_text, pool if pool else None)
    elif provider == "claude":
        t = _call_claude(system_prompt, post_text, api_key)
        return (t, LLM_OK if t else LLM_NETWORK)
    elif provider == "openai":
        t = _call_openai(system_prompt, post_text, api_key)
        return (t, LLM_OK if t else LLM_NETWORK)
    elif provider == "gemini":
        t = _call_gemini(system_prompt, post_text, api_key)
        return (t, LLM_OK if t else LLM_NETWORK)
    else:
        return _call_groq(system_prompt, post_text, [api_key] if api_key else None)


# ── Groq: ротация ключей ─────────────────────────────────────
# Ключи приходят из БД (ServiceCredential), см. get_api_keys_pool().
# При 429/битом ключе — cooldown, при пустом ответе — просто next.
_groq_keys_lock = threading.Lock()
_groq_key_cursor = 0
_groq_key_cooldown: dict[str, float] = {}   # key -> monotonic() when usable again


def _pick_groq_key(pool: list[str], exclude: set) -> str | None:
    """Round-robin, пропускает cooldown и уже пробованные."""
    global _groq_key_cursor
    now = _time.monotonic()
    with _groq_keys_lock:
        n = len(pool)
        for _ in range(n):
            k = pool[_groq_key_cursor % n]
            _groq_key_cursor = (_groq_key_cursor + 1) % n
            if k in exclude:
                continue
            if _groq_key_cooldown.get(k, 0) > now:
                continue
            return k
        return None


def _cooldown_groq_key(k: str, seconds: float, reason: str):
    with _groq_keys_lock:
        _groq_key_cooldown[k] = _time.monotonic() + seconds
    logger.warning(f"Groq key ...{k[-6:]} → cooldown {seconds:.0f}s ({reason})")


# ── Модель Groq ──────────────────────────────────────────────
# llama-3.3-70b-versatile был выведен из обслуживания — все ключи
# получали HTTP 404 model_not_found, generate_comment возвращал ""
# и каждая попытка коммента падала в llm_empty (проверено вживую
# 2026-08-22: 4/4 ключа → 404). Держим список: первая модель основная,
# остальные — авто-fallback если Groq снова снимет модель с обслуживания.
GROQ_MODELS = [
    m.strip() for m in os.getenv(
        "GROQ_MODELS", "openai/gpt-oss-120b,openai/gpt-oss-20b,groq/compound-mini"
    ).split(",") if m.strip()
]

# Модели, снятые с обслуживания в этом процессе (404) — не долбим повторно
_groq_dead_models: set[str] = set()


def _call_groq(system_prompt: str, post_text: str,
               api_keys: list[str] | None = None) -> tuple[str, str]:
    """Ротация ключей и моделей. Возвращает (текст, причина).

    Причина обязательна: без неё вызывающий код писал в лог `llm_empty`
    на любой сбой, и отличить исчерпанную квоту от снятой модели было
    невозможно — см. комментарий к кодам LLM_* выше."""
    import httpx
    # Fallback на env только если пул совсем пуст (для скриптов вне контекста БД)
    pool = list(api_keys or [])
    if not pool:
        env_single = os.getenv("GROQ_API_KEY", "").strip()
        if env_single:
            pool = [env_single]
    if not pool:
        logger.error("Нет Groq API ключей (пул из БД пуст, env тоже пуст)")
        return ("", LLM_NO_KEYS)

    models = [m for m in GROQ_MODELS if m not in _groq_dead_models] or GROQ_MODELS[:1]
    last_err = None

    for model in models:
        tried: set[str] = set()
        model_dead = False
        for _ in range(len(pool)):
            k = _pick_groq_key(pool, tried)
            if k is None:
                break   # все в cooldown или уже пробованы
            tried.add(k)
            try:
                with httpx.Client(timeout=30) as client:
                    resp = client.post(
                        "https://api.groq.com/openai/v1/chat/completions",
                        headers={"Authorization": f"Bearer {k}", "Content-Type": "application/json"},
                        json={
                            "model": model,
                            "max_tokens": 300,
                            "messages": [
                                {"role": "system", "content": system_prompt},
                                {"role": "user", "content": post_text},
                            ],
                        },
                    )
                    if resp.status_code == 429:
                        ra = resp.headers.get("retry-after", "60")
                        try:
                            ra_sec = float(ra)
                        except ValueError:
                            ra_sec = 60.0
                        _cooldown_groq_key(k, ra_sec, "429 rate-limit")
                        last_err = "429"
                        continue
                    if resp.status_code == 401:
                        _cooldown_groq_key(k, 3600, "401 bad-key")
                        last_err = "401"
                        continue
                    if resp.status_code == 404:
                        # Модель снята с обслуживания — вина НЕ ключа.
                        # Помечаем модель мёртвой и уходим на следующую,
                        # не сжигая остальные ключи на том же 404.
                        logger.error(
                            f"Groq: модель {model} недоступна (404) — "
                            f"переключаюсь на следующую из GROQ_MODELS"
                        )
                        _groq_dead_models.add(model)
                        last_err = f"model_not_found:{model}"
                        model_dead = True
                        break
                    resp.raise_for_status()
                    msg = resp.json()["choices"][0]["message"]
                    content = (msg.get("content") or "").strip()
                    # reasoning-модели (gpt-oss) кладут размышления в отдельное
                    # поле reasoning, а в content — чистый ответ. Но если модель
                    # всё же вернула <think>...</think> внутри content — режем.
                    if "</think>" in content:
                        content = content.split("</think>", 1)[1].strip()
                    if content:
                        return (content, LLM_OK)
                    # Провайдер вернул пусто — не cooldown'им ключ, просто пробуем следующий
                    logger.warning(f"Groq key ...{k[-6:]} ({model}) → пустой content, next key")
                    last_err = "empty"
                    continue
            except httpx.HTTPStatusError as e:
                logger.error(f"Groq ...{k[-6:]} ({model}) HTTP {e.response.status_code}: {e.response.text[:200]}")
                last_err = f"http-{e.response.status_code}"
                continue
            except Exception as e:
                logger.error(f"Groq ...{k[-6:]} ({model}): {e}")
                last_err = str(e)[:80]
                continue
        if not model_dead:
            # Модель жива, но все ключи провалили — следующая модель не поможет
            break

    logger.error(f"Groq: не удалось сгенерировать (модели={models}, ключей={len(pool)}, last_err={last_err})")
    # Переводим внутренний last_err в стабильный код причины
    le = str(last_err or "")
    if le == "429":
        reason = LLM_RATE_LIMIT
    elif le == "401":
        reason = LLM_BAD_KEY
    elif le.startswith("model_not_found"):
        reason = LLM_MODEL_404
    elif le == "empty":
        reason = LLM_TRULY_EMPTY
    else:
        reason = LLM_NETWORK
    return ("", reason)


def _call_claude(system_prompt: str, post_text: str, api_key: str = None) -> str:
    import httpx
    if not api_key:
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.error("Нет Claude API ключа (ни в БД, ни в env)")
        return ""
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 300,
                    "system": system_prompt,
                    "messages": [{"role": "user", "content": post_text}],
                },
            )
            resp.raise_for_status()
            for block in resp.json().get("content", []):
                if block.get("type") == "text":
                    return block["text"]
    except Exception as e:
        logger.error(f"Claude: {e}")
    return ""


def _call_openai(system_prompt: str, post_text: str, api_key: str = None) -> str:
    import httpx
    if not api_key:
        api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        logger.error("Нет OpenAI API ключа (ни в БД, ни в env)")
        return ""
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": "gpt-4o",
                    "max_tokens": 300,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": post_text},
                    ],
                },
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        logger.error(f"OpenAI: {e}")
    return ""


def _call_gemini(system_prompt: str, post_text: str, api_key: str = None) -> str:
    import httpx
    if not api_key:
        api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        logger.error("Нет Gemini API ключа (ни в БД, ни в env)")
        return ""
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-lite:generateContent?key={api_key}",
                headers={"Content-Type": "application/json"},
                json={
                    "system_instruction": {"parts": [{"text": system_prompt}]},
                    "contents": [{"parts": [{"text": post_text}]}],
                    "generationConfig": {"maxOutputTokens": 300},
                },
            )
            resp.raise_for_status()
            candidates = resp.json().get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                if parts:
                    return parts[0].get("text", "")
    except Exception as e:
        logger.error(f"Gemini: {e}")
    return ""


def build_comment_prompt(post_text: str, style_profile: dict, personality: dict = None) -> str:
    """Промпт который генерирует РЕАЛЬНЫЕ человеческие комментарии на ЛЮБОМ языке."""

    length = style_profile.get("length", "medium")
    if length == "short":
        len_rule = "Maximum 3-5 words. Like a quick reaction."
    elif length == "long":
        len_rule = "2-3 sentences. Share your opinion on the topic."
    else:
        len_rule = "1 sentence, 10-40 words."

    style_rules = []

    if style_profile.get("uses_emoji"):
        style_rules.append("You can use 1 emoji but not in every comment.")

    if style_profile.get("makes_typos"):
        style_rules.append("Write casually like in a messenger — no caps, skip punctuation sometimes, use slang natural to the post's language.")

    if style_profile.get("asks_question"):
        style_rules.append("Ask a short casual question about the topic.")

    if style_profile.get("starts_with_reply"):
        style_rules.append("React to something specific from the post. Don't use templates — write like a real person.")

    style_block = "\n".join(f"- {r}" for r in style_rules) if style_rules else ""

    prompt = f"""You are writing a comment on a Telegram channel post. You are a regular person scrolling through their feed.

CRITICAL RULE #1: Detect the language of the post and write your comment in THE SAME LANGUAGE. 
- Post in English → comment in English
- Post in Ukrainian → comment in Ukrainian  
- Post in Russian → comment in Russian
- Post in Hindi → comment in Hindi
- Post in Arabic → comment in Arabic
- Post in ANY language → comment in THAT language
Use the same script (Cyrillic, Latin, Devanagari, Arabic etc.)

CRITICAL RULE #2: Write like a REAL person in a messenger, not like an AI or copywriter.

NEVER write anything like these (in any language):
- "Great post!" / "Отличный пост!" / "बहुत अच्छी पोस्ट!"
- "Thanks for sharing" / "Спасибо за информацию" / "शेयर करने के लिए धन्यवाद"
- "Very interesting article" / "Очень интересная статья"
- "I completely agree with the author"
- Any phrase that sounds like a template or AI

How REAL people comment (examples by language):

English: "lol yeah been there", "wait what fr?", "nah I don't think so", "this is so true tho", "bruh 💀", "ok but why", "finally someone said it"

Russian: "ну наконец-то кто-то это сказал", "хз, мне кажется тут не всё так просто", "кста да", "ну такое себе", "а можно подробнее?", "жиза", "я пробовал не работает"

Ukrainian: "о це цікаво", "ну таке", "а шо так можна було?", "хм спірно", "підтримую", "а є пруфи?", "ну нарешті"

Hindi: "sahi baat hai", "ye toh hona hi tha", "koi sense hai iss baat ka?", "haan bhai", "acha point hai", "seriously??"

Arabic: "والله صح", "هذا اللي كنت أقوله", "مو متأكد بصراحة", "يب فعلاً"

Spanish: "jaja tal cual", "eso no tiene sentido", "en serio??", "bueno depende", "al fin alguien lo dice"

{len_rule}

{style_block}

Write ONLY the comment text. No quotes, no explanations, no meta-commentary.

Post:
{post_text}"""

    return prompt