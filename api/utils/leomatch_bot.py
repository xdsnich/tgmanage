"""GramGPT API — utils/leomatch_bot.py
Автоматизация регистрации в @leomatchbot.

Основано на успешной разведке 2026-07-28: acc#210 (Любов Андреева)
прошёл шаги 1-10 полностью. Полный flow регистрации задокументирован
внутри функции onboard_account().

Использование:
    from utils.leomatch_bot import onboard_account, OnboardProfile

    profile = OnboardProfile(
        language="🇺🇦 Українська",
        name="Настя", city="Київ", description="Привіт :)",
        age=25, own_gender="female", seek="male",
        photo_paths=["/uploads/leomatch/1/photo1.jpg"],
    )
    result = await onboard_account(client, profile)
    # result = {"success": bool, "step": int, "log": [...], "error": str}
"""

import asyncio
import re
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

BOT_USERNAME = "@leomatchbot"


# Вероятность что решим ЛАЙКНУТЬ конкретную анкету.
# Остальное — дизлайки. Настоящие пользователи дейтингов ставят ~30-40% лайков,
# 40% — усреднённый естественный процент. Дизлайки нужны чтоб паттерн не был
# "просто нажимает ❤️ подряд" (что триггерит бота).
LIKE_PROBABILITY = 0.4


async def send_likes(client, likes_count: int, stop_check=None) -> dict:
    """Заходит в 'Смотреть анкеты' и на каждую анкету с шансом LIKE_PROBABILITY
    ставит ❤️ или 👎. Останавливается когда набрали `likes_count` лайков ИЛИ
    сделали `likes_count * 3` swipes подряд (защита от бесконечного цикла).

    Flow:
      /start → main menu → '1 🚀' (смотреть анкеты) → бот показывает анкету
      → отправить '❤️' или '👎' → бот шлёт следующую анкету / match / Premium promo
      → повторить.

    Обрабатываем:
      - Premium promo → 'Пока без Premium' и продолжаем
      - Match (Отлично! / Начинай общаться) → это тоже success, продолжаем
      - 'Нет такого варианта ответа' → отправляем повторно
      - 'Больше нет анкет' / 'Закончились' → прерываем

    Returns {"sent": int, "matches": int, "error": str}
    """
    import re, random as _r
    bot_ent = await client.get_entity(BOT_USERNAME)

    async def wait_reply(after_id, tout=15):
        for _ in range(tout * 2):
            await asyncio.sleep(0.5)
            msgs = await client.get_messages(bot_ent, limit=6)
            new = [m for m in msgs if m.id > after_id and not m.out]
            if new:
                return sorted(new, key=lambda x: x.id)
        return []

    def buttons_of(msg):
        rm = getattr(msg, "reply_markup", None)
        if not rm or not getattr(rm, "rows", None):
            return []
        return [(getattr(b, "text", "") or "").strip() for row in rm.rows for b in row.buttons]

    async def current_keyboard():
        """Возвращает последнюю активную reply-клавиатуру бота.
        Reply-клавиатура висит пока не пришло НОВОЕ сообщение с reply_markup.
        Ищем в последних 30 сообщениях бота (leomatch присылает много анкет подряд
        без reply_markup, клавиатура была на далёком `✨🔍`)."""
        msgs = await client.get_messages(bot_ent, limit=30)
        for m in msgs:   # msgs новейшие первыми
            if m.out:
                continue
            btns = buttons_of(m)
            if btns:
                return btns, m
        return [], None

    async def restart_swipe_mode(after_id: int) -> int:
        """Принудительно /start → главное меню → '1 🚀' чтобы получить свежую
        match-клавиатуру. Возвращает обновлённый last_id."""
        await client.send_message(bot_ent, "/start")
        rs = await wait_reply(after_id, tout=15)
        if rs:
            after_id = max(after_id, rs[-1].id)
        # ждём main menu
        for _ in range(6):
            cur_ = (await client.get_messages(bot_ent, limit=1))[0]
            btns_ = buttons_of(cur_)
            if btns_ and any(re.match(r"^1\b|^1[\s\W]", b) for b in btns_):
                btn1 = next(b for b in btns_ if re.match(r"^1\b|^1[\s\W]", b))
                await client.send_message(bot_ent, btn1)
                rs2 = await wait_reply(after_id, tout=15)
                if rs2:
                    after_id = max(after_id, rs2[-1].id)
                return after_id
            await asyncio.sleep(1)
        return after_id

    sent = 0
    matches = 0

    try:
        # /start → main menu → '1 🚀'
        last = await client.get_messages(bot_ent, limit=1)
        last_id = last[0].id if last else 0
        await client.send_message(bot_ent, "/start")
        rs = await wait_reply(last_id, tout=15)
        if not rs:
            return {"sent": 0, "matches": 0, "error": "no reply to /start"}
        last_id = max(last_id, rs[-1].id)

        # ждём пока не появится кнопка '1' (main menu)
        cur = (await client.get_messages(bot_ent, limit=1))[0]
        for _ in range(6):
            btns = buttons_of(cur)
            if btns and any(re.match(r"^1\b|^1[\s\W]", b) for b in btns):
                break
            await asyncio.sleep(1)
            cur = (await client.get_messages(bot_ent, limit=1))[0]
        _init_btns = buttons_of(cur)
        # Language selector — акк лишён анкеты (был сброшен ботом)
        if any(re.search(r"^🇬🇧|^🇷🇺|^🇺🇦|^🇰🇿|^🇵🇱", b) for b in _init_btns):
            return {"sent": 0, "matches": 0, "error": "account_reset_to_language"}
        btn_1 = next((b for b in _init_btns if re.match(r"^1\b|^1[\s\W]", b)), None)
        if not btn_1:
            return {"sent": 0, "matches": 0, "error": f"no btn '1', got {_init_btns}"}
        await client.send_message(bot_ent, btn_1)
        rs = await wait_reply(last_id)
        for r in rs: last_id = max(last_id, r.id)

        # Цикл: swipes пока не набрали `likes_count` лайков (или не превысили лимит попыток).
        max_swipes = likes_count * 3   # если лайков 8 → максимум 24 свайпа
        swipes_done = 0
        dislikes_sent = 0

        while sent < likes_count and swipes_done < max_swipes:
            # FORCE STOP check — юзер нажал Stop в UI
            if stop_check and stop_check():
                return {"sent": sent, "matches": matches,
                        "dislikes": dislikes_sent, "swipes": swipes_done,
                        "error": "stopped_by_user"}
            swipes_done += 1
            # Пауза 3-8 сек между свайпами (человекоподобно)
            await asyncio.sleep(_r.uniform(3, 8))

            # Решаем — ❤️ или 👎 (40% лайк / 60% дизлайк)
            do_like = _r.random() < LIKE_PROBABILITY

            # Стратегия по клавиатуре:
            #  A) Клавиатура есть и содержит ❤ → жмём её точный текст
            #  B) Клавиатура есть но НЕ match-screen (popup/menu) → dismiss loop
            #  C) Клавиатура пуста в 30 сообщениях → шлём ❤️/👎 ВСЛЕПУЮ
            #     (leomatch присылает много анкет подряд без reply_markup,
            #      клавиатура ✨🔍[❤️👎...] висит с далёкого сообщения)
            heart_btn = None
            dislike_actual = "👎"
            cur_btns = []
            for _try in range(5):
                cur_btns, _ = await current_keyboard()
                heart_btn = next((b for b in cur_btns if b.startswith("❤")), None)
                if heart_btn:
                    dislike_actual = next((b for b in cur_btns if b.startswith("👎")), "👎")
                    break
                # Case C: клавиатура пуста → оптимистично шлём ❤️/👎 вслепую
                if not cur_btns:
                    heart_btn = "❤️"
                    dislike_actual = "👎"
                    break

                # Проверяем не кончились ли анкеты
                last_bot = await client.get_messages(bot_ent, limit=1)
                if last_bot and re.search(r"больше нет анкет|закончил|нет новых|нет.*подходящ",
                                          (last_bot[0].message or "").lower()):
                    return {"sent": sent, "matches": matches, "error": "no_more_profiles"}

                # Language selector — акк лишён анкеты
                if any(re.search(r"^🇬🇧|^🇷🇺|^🇺🇦|^🇰🇿|^🇵🇱", b) for b in cur_btns):
                    return {"sent": sent, "matches": matches,
                            "error": "account_reset_to_language"}

                # Verification prompt в цикле: бот показывает "видео-кружок дайвинчик"
                # с одной кнопкой [Пропустить]. Смотрим в последних 5 сообщениях
                # (бот часто шлёт verification и следом ещё сообщение).
                if len(cur_btns) == 1 and re.search(
                        r"пропуст|skip|позже", cur_btns[0], re.IGNORECASE):
                    try:
                        recent = await client.get_messages(bot_ent, limit=5)
                        combined = " || ".join((m.message or "") for m in recent if not m.out).lower()
                        if re.search(r"верификац|видео.?кружок|дайвинчик|для верифи",
                                     combined):
                            return {"sent": sent, "matches": matches,
                                    "error": "verification_required"}
                    except Exception:
                        pass

                # priority dismiss (skip > back > accept > resume-1)
                skip_btn = next((b for b in cur_btns if re.search(
                    r"позже|later|skip|пропуст|не интерес|не цікав|не хочу|не сейчас|no thanks|"
                    r"пока без|без premium|no premium",
                    b, re.IGNORECASE)), None)
                back_btn = next((b for b in cur_btns if re.search(
                    r"назад|отмен|back|cancel", b, re.IGNORECASE)), None)
                accept_btn = next((b for b in cur_btns if re.search(
                    r"^\W*готово\W*$|^\W*ок\W*$|^\W*ok\W*$|^\W*done\W*$", b, re.IGNORECASE)), None)
                resume_btn = next((b for b in cur_btns if re.match(r"^1\b|^1[\s\W]", b)), None)
                target = skip_btn or back_btn or accept_btn or resume_btn
                if not target:
                    # Радикальный fallback — /start → главное меню → "1 🚀"
                    last_id = await restart_swipe_mode(last_id)
                    continue
                await client.send_message(bot_ent, target)
                rs_r = await wait_reply(last_id, tout=15)
                for rr in rs_r: last_id = max(last_id, rr.id)
                await asyncio.sleep(2)

            if not heart_btn:
                return {"sent": sent, "matches": matches,
                        "error": f"pre-like stuck: keyboard {cur_btns}"}

            action_btn = heart_btn if do_like else dislike_actual

            await client.send_message(bot_ent, action_btn)
            rs = await wait_reply(last_id, tout=20)
            if not rs:
                return {"sent": sent, "matches": matches,
                        "error": f"no reply after swipe #{swipes_done} ({action_btn})"}
            last_id = max(last_id, rs[-1].id)
            if do_like:
                sent += 1
            else:
                dislikes_sent += 1

            # Анализируем ответ
            responded_texts = " || ".join((m.message or "") for m in rs).lower()

            # 1. Match! ("отлично!", "начинай общаться", "взаимная симпатия") — только для лайков
            match_happened = False
            if do_like and re.search(r"начинай общаться|взаимн|отлично.*надеюсь|поздравл", responded_texts):
                matches += 1
                match_happened = True

            # 2. Premium promo — надо нажать "Пока без Premium", затем скорее всего
            #    бот вернёт в главное меню (1..5) → надо повторно жмякнуть "1 🚀"
            for r in rs:
                for b in buttons_of(r):
                    if re.search(r"пока без premium|без премиум|no premium", b, re.IGNORECASE):
                        await client.send_message(bot_ent, b)
                        rs2 = await wait_reply(last_id, tout=10)
                        for rr in rs2: last_id = max(last_id, rr.id)
                        # После "Пока без Premium" бот показал главное меню?
                        cur_after = (await client.get_messages(bot_ent, limit=1))[0]
                        after_btns = buttons_of(cur_after)
                        back_to_swipe = next((bb for bb in after_btns if re.match(r"^1\b|^1[\s\W]", bb)), None)
                        if back_to_swipe:
                            await client.send_message(bot_ent, back_to_swipe)
                            rs3 = await wait_reply(last_id, tout=10)
                            for rr in rs3: last_id = max(last_id, rr.id)
                        break
                else:
                    continue
                break

            # 2b. После match reply-клавиатура обычно снимается (остаётся inline
            # "Пожаловаться"). Принудительно /start → 1 🚀 чтобы вернуть свайп-режим.
            if match_happened:
                last_id = await restart_swipe_mode(last_id)

            # 3. Проверить не кончились ли анкеты
            if re.search(r"больше нет анкет|закончил|нет новых|попроб.*позже|нет.*подходящ", responded_texts):
                return {"sent": sent, "matches": matches, "error": "no_more_profiles"}

            # 4. "Нет такого варианта" — клавиатура сменилась. Пробуем восстановить.
            #    Смотрим НЕ последнее сообщение, а последнее с reply_markup
            #    (reply-клавиатура висит от старого сообщения).
            if "нет такого варианта" in responded_texts:
                for _try in range(3):
                    btns, kbd_msg = await current_keyboard()
                    # Приоритет: skip > back > accept > resume-1
                    skip_btn = next((b for b in btns if re.search(
                        r"позже|later|skip|пропуст|не интерес|не цікав|не хочу|не сейчас|no thanks|"
                        r"пока без|без premium|no premium",
                        b, re.IGNORECASE)), None)
                    back_btn = next((b for b in btns if re.search(
                        r"назад|отмен|back|cancel", b, re.IGNORECASE)), None)
                    accept_btn = next((b for b in btns if re.search(
                        r"^\W*готово\W*$|^\W*ок\W*$|^\W*ok\W*$|^\W*done\W*$", b, re.IGNORECASE)), None)
                    resume_btn = next((b for b in btns if re.match(r"^1\b|^1[\s\W]", b)), None)
                    target = skip_btn or back_btn or accept_btn
                    if target:
                        await client.send_message(bot_ent, target)
                        rs2 = await wait_reply(last_id, tout=10)
                        for rr in rs2: last_id = max(last_id, rr.id)
                        await asyncio.sleep(1)
                        continue
                    if resume_btn:
                        await client.send_message(bot_ent, resume_btn)
                        rs2 = await wait_reply(last_id, tout=10)
                        for rr in rs2: last_id = max(last_id, rr.id)
                        break   # вернулись в match-screen — продолжаем цикл лайков
                    # ничего не помогло — прерываем
                    return {"sent": sent, "matches": matches, "error": f"stuck: keyboard {btns}"}

        return {"sent": sent, "matches": matches, "dislikes": dislikes_sent,
                "swipes": swipes_done, "error": ""}
    except Exception as e:
        return {"sent": sent, "matches": matches,
                "dislikes": locals().get("dislikes_sent", 0),
                "swipes": locals().get("swipes_done", 0),
                "error": f"{type(e).__name__}: {str(e)[:200]}"}


async def replace_disabled_photo(client, new_photo_path: str) -> dict:
    """Для акка у которого анкета отключена ('Твоя анкета была отключена из-за
    неподходящего фото') меняет фото на новое.

    Flow: /start → main menu → '3' (изменить фото) → отправить фото →
          'Это все, сохранить фото' → ждать 'Сохранили изменения'.

    Returns {"success": bool, "step": str, "error": str, "final_bot_text": str}
    """
    import re
    bot_ent = await client.get_entity(BOT_USERNAME)

    async def wait_reply(after_id, tout=20):
        for _ in range(tout * 2):
            await asyncio.sleep(0.5)
            msgs = await client.get_messages(bot_ent, limit=6)
            new = [m for m in msgs if m.id > after_id and not m.out]
            if new:
                return sorted(new, key=lambda x: x.id)
        return []

    def buttons(msg):
        rm = getattr(msg, "reply_markup", None)
        if not rm or not getattr(rm, "rows", None):
            return []
        return [(getattr(b, "text", "") or "").strip() for row in rm.rows for b in row.buttons]

    try:
        # /start
        last = (await client.get_messages(bot_ent, limit=1))
        last_id = last[0].id if last else 0
        await client.send_message(bot_ent, "/start")
        rs = await wait_reply(last_id, tout=15)
        if not rs:
            return {"success": False, "step": "start", "error": "no reply", "final_bot_text": ""}
        last_id = max(last_id, rs[-1].id)

        # Ждём главное меню (1..5)
        for _ in range(8):
            await asyncio.sleep(1)
            cur = (await client.get_messages(bot_ent, limit=1))[0]
            btns = buttons(cur)
            if btns and any(re.match(r"^3\b|^3[\s\W]", b) for b in btns):
                break
        else:
            return {"success": False, "step": "main_menu", "error": "menu 1..5 not appeared",
                    "final_bot_text": (cur.message or "")[:200]}

        # Click "3" (изменить фото/видео)
        btn_3 = next((b for b in buttons(cur) if re.match(r"^3\b|^3[\s\W]", b)), None)
        if not btn_3:
            return {"success": False, "step": "click_3", "error": "no btn 3", "final_bot_text": ""}
        await client.send_message(bot_ent, btn_3)
        rs = await wait_reply(last_id)
        if not rs:
            return {"success": False, "step": "after_3", "error": "no reply after 3", "final_bot_text": ""}
        last_id = max(last_id, rs[-1].id)

        # Отправляем фото
        await client.send_file(bot_ent, new_photo_path)
        rs = await wait_reply(last_id, tout=30)
        if not rs:
            return {"success": False, "step": "photo_reply", "error": "no reply after photo", "final_bot_text": ""}
        last_id = max(last_id, rs[-1].id)

        # Нажать "Это все, сохранить фото"
        cur = (await client.get_messages(bot_ent, limit=1))[0]
        btn_save = next((b for b in buttons(cur) if re.search(r"сохранить|это все|достаточно", b, re.IGNORECASE)), None)
        if not btn_save:
            return {"success": False, "step": "save_btn",
                    "error": f"no save btn, buttons={buttons(cur)}",
                    "final_bot_text": (cur.message or "")[:200]}
        await client.send_message(bot_ent, btn_save)
        rs = await wait_reply(last_id, tout=20)
        if not rs:
            return {"success": False, "step": "after_save", "error": "no reply after save", "final_bot_text": ""}

        # Ищем маркер успеха 'Сохранили изменения' в новых сообщениях
        for m in rs:
            if re.search(r"сохранили изменения|збережено|saved", m.message or "", re.IGNORECASE):
                return {"success": True, "step": "done", "error": "", "final_bot_text": m.message[:200]}
        return {"success": False, "step": "no_confirm",
                "error": f"no 'saved' marker, got: {[(m.message or '')[:80] for m in rs]}",
                "final_bot_text": (rs[-1].message or "")[:200]}
    except Exception as e:
        return {"success": False, "step": "exception",
                "error": f"{type(e).__name__}: {str(e)[:200]}", "final_bot_text": ""}


async def fix_privacy_for_leomatch(client) -> dict:
    """Меняет privacy settings акка чтобы @leomatchbot мог показывать анкету:
    - Forwards: "Кто может ссылаться на мой аккаунт" → Все
      (без этого бот показывает "Твой профиль ограничен из-за настроек приватности")

    Returns {"success": bool, "error": str}
    """
    try:
        from telethon.tl.functions.account import SetPrivacyRequest
        from telethon.tl.types import InputPrivacyKeyForwards, InputPrivacyValueAllowAll
        await client(SetPrivacyRequest(
            key=InputPrivacyKeyForwards(),
            rules=[InputPrivacyValueAllowAll()],
        ))
        return {"success": True, "error": ""}
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}

# Regex — русский + украинский. Обновлять при появлении новых языков.
GENDER_MALE_RE = r"парн|парен|мужч|male|man|хлопц|хлопець|чолов"
GENDER_FEMALE_RE = r"девуш|девчонк|женщ|дівч|female|woman|жін"


@dataclass
class OnboardProfile:
    language: str = "🇺🇦 Українська"
    name: str = ""              # если пусто — берём first_name TG-акка
    city: str = "Київ"
    description: str = ""       # пусто → "Пропустити" если есть, иначе прочерк
    age: int = 25
    own_gender: str = "female"  # "male" | "female" | "auto" (auto → детектим по first_name)
    seek: str = "male"          # "male" | "female" | "any"
    photo_paths: list[str] = field(default_factory=list)


def _dump_msg(m) -> dict:
    d = {
        "id": m.id,
        "text": (m.message or m.text or "")[:500],
        "has_photo": bool(m.photo),
        "kb_type": None,
        "buttons": [],
    }
    rm = getattr(m, "reply_markup", None)
    if rm is None:
        return d
    d["kb_type"] = type(rm).__name__
    for row in getattr(rm, "rows", []) or []:
        for btn in getattr(row, "buttons", []) or []:
            d["buttons"].append({
                "type": type(btn).__name__,
                "text": getattr(btn, "text", ""),
            })
    return d


def _pick_button(msg: dict, want_re: str = None, want_exact: str = None) -> Optional[str]:
    for b in msg["buttons"]:
        t = b["text"] or ""
        if want_exact and t.strip().lower() == want_exact.strip().lower():
            return t
        if want_re and re.search(want_re, t, re.IGNORECASE):
            return t
    return None


async def _wait_reply(client, bot_ent, after_id: int, timeout_sec: int = 25) -> list[dict]:
    for _ in range(timeout_sec * 2):
        await asyncio.sleep(0.5)
        msgs = await client.get_messages(bot_ent, limit=6)
        new = [m for m in msgs if m.id > after_id and m.out is False]
        if new:
            return [_dump_msg(m) for m in sorted(new, key=lambda x: x.id)]
    return []


async def onboard_account(client, profile: OnboardProfile) -> dict:
    """Пройти полный флоу @leomatchbot для подключённого клиента.

    Args:
        client: подключённый Telethon TelegramClient (уже .connect() + is_user_authorized)
        profile: OnboardProfile с параметрами

    Returns:
        dict {success: bool, step: int (last), log: [...], error: str}
    """
    log: list[dict] = []
    error = ""

    def _log(step: int, bot_msg: dict = None, action: str = "", note: str = ""):
        entry = {"step": step, "action": action, "note": note}
        if bot_msg:
            entry["bot_text"] = bot_msg["text"][:200]
            entry["bot_buttons"] = [b["text"] for b in bot_msg["buttons"]]
        log.append(entry)

    try:
        me = await client.get_me()
        bot_ent = await client.get_entity(BOT_USERNAME)

        # Определяем имя
        name = profile.name.strip() or (me.first_name or "").strip() or "Тест"
        # own_gender=auto → грубый детект по имени (не идеально, но лучше чем ничего)
        own_gender = profile.own_gender
        if own_gender == "auto":
            fn = (me.first_name or "").lower()
            female_endings = ("а", "я", "ія", "ева", "ова", "ина")
            own_gender = "female" if fn.endswith(female_endings) else "male"

        # Последнее сообщение — точка отсчёта
        prev = await client.get_messages(bot_ent, limit=1)
        last_id = prev[0].id if prev else 0

        await client.send_message(bot_ent, "/start")
        _log(0, action="/start")

        replies = await _wait_reply(client, bot_ent, last_id, timeout_sec=15)
        if not replies:
            return {"success": False, "step": 0, "log": log, "error": "bot did not reply to /start"}
        for r in replies:
            last_id = max(last_id, r["id"])

        photo_uploaded_count = 0
        MAX_STEPS = 30

        for step in range(1, MAX_STEPS + 1):
            cur = await client.get_messages(bot_ent, limit=1)
            if not cur:
                break
            m = _dump_msg(cur[0])
            # Если бот прислал сообщение БЕЗ кнопок — вероятно это preview анкеты
            # ("Andriy, 26, Венеция") или "Так выглядит твоя анкета:", а следом придёт
            # реальный вопрос с кнопками. Даём боту 5 секунд, потом перечитываем.
            if not m["buttons"]:
                await asyncio.sleep(5)
                cur2 = await client.get_messages(bot_ent, limit=1)
                if cur2 and cur2[0].id != m["id"]:
                    m = _dump_msg(cur2[0])
            txt = (m["text"] or "").lower()

            # -1. Если акк УЖЕ зарегистрирован — бот показывает главное меню
            # ("1. Смотреть анкеты. 2. Заполнить заново..." с кнопками 1..5).
            # Считаем это success — регистрация была ранее.
            if re.search(r"смотреть анкет|дивитись анкет|watch profiles|заполнить анкет|заповн.*анкет", txt) and \
               any(re.match(r"\d[\s🚀⭐️]*", (b["text"] or "").strip()) for b in m["buttons"]):
                _log(step, m, action="already registered (main menu detected)")
                return {"success": True, "step": step, "log": log, "error": ""}

            # 0. Язык — определяем НЕ по тексту (может быть на любом языке),
            # а по количеству кнопок-флагов (у @leomatchbot их всегда 12).
            _flag_re = r"[🇦-🇿]{2,}|🇬🇧|🇷🇺|🇺🇦|🇰🇿|🇮🇩|🇲🇾|🇪🇸|🇵🇱|🇮🇹|🇵🇹|🇺🇿|🇰🇭"
            _flag_btns = [b for b in m["buttons"] if re.search(_flag_re, b["text"] or "")]
            if len(_flag_btns) >= 5:
                b = _pick_button(m, want_exact=profile.language)
                if not b:
                    b = _pick_button(m, want_re=r"українська|русский|english|українська")
                if not b:
                    # fallback — первая доступная кнопка с флагом
                    b = _flag_btns[0]["text"]
                _log(step, m, action=f"lang → {b!r}")
                await client.send_message(bot_ent, b)
                rs = await _wait_reply(client, bot_ent, last_id)
                for r in rs: last_id = max(last_id, r["id"])
                continue

            # 1. Приветствие / Ok / Продолжить
            b = _pick_button(m, want_re=(
                r"согласен|продовж|продолж|начать|поч|заполн|заповн|створ|"
                r"создать|поехали|поїхали|давай|^\W*ok\W*$|^\W*ок\W*$|далі|далее"
            ))
            if b:
                _log(step, m, action=f"ok → {b!r}")
                await client.send_message(bot_ent, b)
                rs = await _wait_reply(client, bot_ent, last_id)
                for r in rs: last_id = max(last_id, r["id"])
                continue

            # 2. Возраст
            if re.search(r"возраст|сколько.*лет|вік|скільки.*років", txt):
                _log(step, m, action=f"age → {profile.age}")
                await client.send_message(bot_ent, str(profile.age))
                rs = await _wait_reply(client, bot_ent, last_id)
                for r in rs: last_id = max(last_id, r["id"])
                continue

            # 3. Свой пол (RU: "определимся с полом" / "твой пол" / UA: "стать")
            if re.search(r"стать|пол(ом|а|у|е)?\b|определ.*пол|укажи.*пол|твой пол|ваш пол|оберемо стать|обери стать", txt):
                want = GENDER_FEMALE_RE if own_gender == "female" else GENDER_MALE_RE
                b = _pick_button(m, want_re=want)
                if b:
                    _log(step, m, action=f"gender → {b!r}")
                    await client.send_message(bot_ent, b)
                    rs = await _wait_reply(client, bot_ent, last_id)
                    for r in rs: last_id = max(last_id, r["id"])
                    continue

            # 4. Кого ищет
            if re.search(r"ищ.?шь|кого.*ищ|кого шука|кого ти шука|інтерес|цікав|кто тебе интерес|кого хочешь|кого показ|кому показ", txt):
                if profile.seek == "any":
                    b = _pick_button(m, want_re=r"не важно|обо?их|всё равно|не має значен|байдуже|все одно")
                else:
                    want = GENDER_MALE_RE if profile.seek == "male" else GENDER_FEMALE_RE
                    b = _pick_button(m, want_re=want)
                if b:
                    _log(step, m, action=f"seek → {b!r}")
                    await client.send_message(bot_ent, b)
                    rs = await _wait_reply(client, bot_ent, last_id)
                    for r in rs: last_id = max(last_id, r["id"])
                    continue

            # 5. Город
            if re.search(r"город|где.*живё?ш|міст|де.*живе?ш|з якого", txt):
                _log(step, m, action=f"city → {profile.city!r}")
                await client.send_message(bot_ent, profile.city)
                rs = await _wait_reply(client, bot_ent, last_id)
                for r in rs: last_id = max(last_id, r["id"])
                continue

            # 6. Имя (RU: "как тебя зовут / называть", UA: "як тебе звати / називати")
            if re.search(r"имя|как.*теб.?.*зов|как.*теб.?.*назыв|ім'я|як.*тебе.*звати|як.*тебе.*назив|звер|обращ|называ|назв", txt):
                # Часто бот предлагает готовое имя (TG first_name) кнопкой
                if profile.name.strip():
                    _log(step, m, action=f"name text → {name!r}")
                    await client.send_message(bot_ent, name)
                else:
                    btn = _pick_button(m, want_re=r".+")
                    if btn:
                        _log(step, m, action=f"name btn → {btn!r}")
                        await client.send_message(bot_ent, btn)
                    else:
                        _log(step, m, action=f"name text → {name!r}")
                        await client.send_message(bot_ent, name)
                rs = await _wait_reply(client, bot_ent, last_id)
                for r in rs: last_id = max(last_id, r["id"])
                continue

            # 7. Описание
            if re.search(r"расскажи|о себе|напиши.*себ|описа|розкажи|про себе|опис", txt):
                desc = profile.description.strip()
                if desc:
                    _log(step, m, action=f"desc → {desc!r}")
                    await client.send_message(bot_ent, desc)
                else:
                    b = _pick_button(m, want_re=r"пропустити|пропустить|далі|skip")
                    if b:
                        _log(step, m, action=f"desc skip → {b!r}")
                        await client.send_message(bot_ent, b)
                    else:
                        _log(step, m, action="desc text →  '-'")
                        await client.send_message(bot_ent, "-")
                rs = await _wait_reply(client, bot_ent, last_id)
                for r in rs: last_id = max(last_id, r["id"])
                continue

            # 8. Фото. Дайвинчик СНАЧАЛА присылает "надішли фото" — грузим 1.
            # Потом присылает СНОВА "надішли ще" (без кнопки готово). Стратегия:
            # 1) если пул не исчерпан — берём следующее из пула
            # 2) если пул исчерпан — жмём кнопку "готово"/"опубликовать"/"дальше"
            # 3) если и её нет — шлём /finish (leomatch поддерживает эту команду)
            # 4) если и это не помогло — переиспользуем первое фото
            if re.search(r"фото|отправ.*картинк|аватар|надішли.*фото|фотогр|надішли ще|опубл", txt):
                if photo_uploaded_count >= len(profile.photo_paths):
                    b = _pick_button(m, want_re=r"готов|далі|далее|закінчи|заверш|продолж|продовж|опубл|публик|дальше|сохранить|збереж|это все|це все|достаточно")
                    if b:
                        _log(step, m, action=f"photo done btn → {b!r}")
                        await client.send_message(bot_ent, b)
                        rs = await _wait_reply(client, bot_ent, last_id)
                        for r in rs: last_id = max(last_id, r["id"])
                        continue
                    # Пробуем /finish (тайная команда leomatch) — если сработает, ok
                    if not any(s.get("action", "").startswith("/finish") for s in log[-3:]):
                        _log(step, m, action="/finish (пул исчерпан, нет кнопки)")
                        await client.send_message(bot_ent, "/finish")
                        rs = await _wait_reply(client, bot_ent, last_id, timeout_sec=15)
                        for r in rs: last_id = max(last_id, r["id"])
                        continue
                    # Финальный fallback: переиспользуем первое фото ещё раз
                    p = profile.photo_paths[0]
                    _log(step, m, action=f"photo reuse (пул исчерпан) → {Path(p).name}")
                    await client.send_file(bot_ent, p)
                    photo_uploaded_count += 1
                    rs = await _wait_reply(client, bot_ent, last_id, timeout_sec=35)
                    for r in rs: last_id = max(last_id, r["id"])
                    continue
                p = profile.photo_paths[photo_uploaded_count]
                if not Path(p).exists():
                    error = f"фото не найдено: {p}"
                    _log(step, m, note=error)
                    return {"success": False, "step": step, "log": log, "error": error}
                _log(step, m, action=f"photo → {Path(p).name}")
                await client.send_file(bot_ent, p)
                photo_uploaded_count += 1
                rs = await _wait_reply(client, bot_ent, last_id, timeout_sec=35)
                for r in rs: last_id = max(last_id, r["id"])
                continue

            # 8a. Верификация номера телефона (KeyboardButtonRequestPhone).
            # Дайвинчик требует поделиться контактом ПОСЛЕ фото.
            if re.search(r"отправ.*номер|поделит.*номер|нажми.*номер|поділит.*номер|надішли.*номер", txt) or \
               any(re.search(r"номер телефон|телефон", b["text"] or "", re.IGNORECASE) for b in m["buttons"]):
                _log(step, m, action=f"send_contact (phone={me.phone})")
                from telethon.tl.types import InputMediaContact
                from telethon.tl.functions.messages import SendMediaRequest
                import random as _rnd
                try:
                    await client(SendMediaRequest(
                        peer=bot_ent,
                        media=InputMediaContact(
                            phone_number=me.phone or "",
                            first_name=me.first_name or "",
                            last_name=me.last_name or "",
                            vcard="",
                        ),
                        message="",
                        random_id=_rnd.randint(1, 2**63),
                    ))
                except Exception as e:
                    error = f"send_contact failed: {type(e).__name__}: {str(e)[:120]}"
                    _log(step, m, note=error)
                    return {"success": False, "step": step, "log": log, "error": error}
                rs = await _wait_reply(client, bot_ent, last_id, timeout_sec=20)
                for r in rs: last_id = max(last_id, r["id"])
                continue

            # 8b. Подтверждение анкеты — "Все верно?" / "Все вірно?"
            if re.search(r"все верно|усе вірно|усё правильно|верно\?", txt):
                b = _pick_button(m, want_re=r"^\W*да\W*$|^\W*так\W*$|верно|вірно|подтвер|підтвер")
                if b:
                    _log(step, m, action=f"confirm → {b!r}")
                    await client.send_message(bot_ent, b)
                    await asyncio.sleep(2)
                    return {"success": True, "step": step, "log": log, "error": ""}

            # 9. Main menu / match-screen (♥️ 👎 💤) = регистрация завершена
            _match_btns = {"❤️", "❤", "👎", "💤", "💌", "🔥"}
            if any((b["text"] or "").strip() in _match_btns or any(e in (b["text"] or "") for e in _match_btns)
                   for b in m["buttons"]):
                _log(step, m, action="reached main menu (match-screen)")
                return {"success": True, "step": step, "log": log, "error": ""}

            # 10. Финал: поиск / анкеты / готово (legacy fallback)
            b = _pick_button(m, want_re=r"поиск|искать|начать|готов|дивит|анкет|шукати|перегляд")
            if b and step > 5:
                _log(step, m, action=f"final → {b!r}")
                await client.send_message(bot_ent, b)
                await asyncio.sleep(2)
                return {"success": True, "step": step, "log": log, "error": ""}

            # Fallback: если бот прислал одну кнопку — жмём её (после step > 6)
            if len(m["buttons"]) == 1 and step > 6:
                b = m["buttons"][0]["text"]
                _log(step, m, action=f"single-btn fallback → {b!r}")
                await client.send_message(bot_ent, b)
                rs = await _wait_reply(client, bot_ent, last_id)
                for r in rs: last_id = max(last_id, r["id"])
                continue

            # Не поняли шаг
            error = f"unknown step: bot_text={m['text'][:100]!r} buttons={[b['text'] for b in m['buttons']]}"
            _log(step, m, note=error)
            return {"success": False, "step": step, "log": log, "error": error}

        return {"success": False, "step": MAX_STEPS, "log": log, "error": "MAX_STEPS reached"}

    except Exception as e:
        logger.exception("onboard_account fatal")
        return {"success": False, "step": -1, "log": log, "error": f"{type(e).__name__}: {str(e)[:200]}"}
