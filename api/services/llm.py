"""
GramGPT — services/llm.py
LLM провайдеры для генерации комментариев.
Используется в commenting_tasks.py и run_listener.py
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
LLM_MAX_PER_MINUTE = 10


def _check_rate_limit() -> bool:
    global _last_call_time
    with _llm_lock:
        now = _time.time()
        _llm_calls[:] = [t for t in _llm_calls if now - t < 60]
        if len(_llm_calls) >= LLM_MAX_PER_MINUTE:
            logger.warning(f"Rate limit: {len(_llm_calls)}/{LLM_MAX_PER_MINUTE}/мин")
            return False
        time_since = now - _last_call_time
        if time_since < 4.0:
            _time.sleep(4.0 - time_since)
        current = _time.time()
        _llm_calls.append(current)
        _last_call_time = current
        return True


# ── Провайдеры ───────────────────────────────────────────────

def generate_comment(provider: str, system_prompt: str, post_text: str) -> str:
    """Генерирует комментарий через выбранный LLM."""
    if not _check_rate_limit():
        return ""
    if provider == "groq":
        return _call_groq(system_prompt, post_text)
    elif provider == "claude":
        return _call_claude(system_prompt, post_text)
    elif provider == "openai":
        return _call_openai(system_prompt, post_text)
    elif provider == "gemini":
        return _call_gemini(system_prompt, post_text)
    else:
        return _call_groq(system_prompt, post_text)


def _call_groq(system_prompt: str, post_text: str) -> str:
    import httpx
    api_key = os.getenv("GROQ_API_KEY", "")
    if not api_key:
        logger.error("GROQ_API_KEY не задан!")
        return ""
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": "llama-3.3-70b-versatile",
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
        logger.error(f"Groq: {e}")
    return ""


def _call_claude(system_prompt: str, post_text: str) -> str:
    import httpx
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.error("ANTHROPIC_API_KEY не задан!")
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


def _call_openai(system_prompt: str, post_text: str) -> str:
    import httpx
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        logger.error("OPENAI_API_KEY не задан!")
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


def _call_gemini(system_prompt: str, post_text: str) -> str:
    import httpx
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        logger.error("GEMINI_API_KEY не задан!")
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
