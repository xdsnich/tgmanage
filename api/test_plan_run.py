"""
GramGPT — test_plan_run.py
Прогон РЕАЛЬНОГО мини-плана на 2 аккаунтах параллельно.

Что делает (для каждого аккаунта):
  1. Подключается через Telethon (с прокси)
  2. Ждёт N секунд (имитация запланированной сессии connect_at_hour)
  3. JOIN канала — с верификацией через GetParticipantRequest как в plan_executor
  4. read_feed — прочитать последние 3 поста
  5. set_reaction на последний пост (берёт available_reactions канала)
  6. (Опционально) выйти из канала через --leave
  7. Отключается

Это полный mini-end-to-end: db_pool + account_lock + Telethon + проксі +
вся новая логика подписки (resolve → join → verify) + реакции.

Запуск:
  cd api
  python test_plan_run.py @durov                   # первые 2 активных
  python test_plan_run.py @durov 5 7               # конкретные ID
  python test_plan_run.py @durov 5 7 --wait 10     # с задержкой 10с
  python test_plan_run.py @durov 5 7 --leave       # выйти из канала после
  python test_plan_run.py @durov 5 7 --serial      # последовательно (для сравнения)

ВНИМАНИЕ: реально вступает и оставляет реакцию. Тесть на каком-нибудь публичном
канале (например @durov), который не жалко. Или используй --leave чтобы выйти после.
"""

import sys
import os
import asyncio
import argparse
import random
import time
import logging

# sys.path: api/ — первым, parent (tg_manager1/ с легаси config.py) — убрать
_API_DIR    = os.path.dirname(os.path.abspath(__file__))
_PARENT_DIR = os.path.dirname(_API_DIR)
sys.path = [p for p in sys.path
            if os.path.normcase(os.path.abspath(p) if p else "") != os.path.normcase(_PARENT_DIR)]
if _API_DIR not in sys.path:
    sys.path.insert(0, _API_DIR)

logging.basicConfig(level=logging.WARNING, format='[%(levelname)s] %(name)s: %(message)s')


class C:
    R = "\033[31m"; G = "\033[32m"; Y = "\033[33m"; B = "\033[34m"
    C = "\033[36m"; M = "\033[35m"; BOLD = "\033[1m"; DIM = "\033[2m"; OFF = "\033[0m"


def step_ok(label, detail=""):
    print(f"  {C.G}✓{C.OFF} {label}{C.DIM} — {detail}{C.OFF}" if detail else f"  {C.G}✓{C.OFF} {label}")

def step_fail(label, err="", err_type=""):
    type_str = f"{C.Y}[{err_type}]{C.OFF} " if err_type else ""
    print(f"  {C.R}✕{C.OFF} {label} → {type_str}{C.R}{err}{C.OFF}")

def step_info(label, detail=""):
    print(f"  {C.B}ℹ{C.OFF} {label}{C.DIM} — {detail}{C.OFF}" if detail else f"  {C.B}ℹ{C.OFF} {label}")

def header(text):
    print(f"\n{C.M}{C.BOLD}═══ {text} ═══{C.OFF}")


# ─────────────────────────────────────────────────────────
# Мини-план для одного аккаунта
# ─────────────────────────────────────────────────────────
async def run_mini_plan(account_id: int, channel_username: str,
                         wait_seconds: int = 0, leave_after: bool = False):
    """Прогоняет: connect → wait → join+verify → read_feed → set_reaction → (leave)."""
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload
    from utils.db_pool import async_session as Session
    from utils.account_lock import acquire_account_lock, release_account_lock
    from utils.telegram import make_telethon_client
    from models.account import TelegramAccount
    from models.proxy import Proxy

    result = {"account_id": account_id, "phone": "?", "steps": [], "ok": False, "elapsed": 0}
    started = time.time()
    clean_ch = channel_username.lstrip('@').strip()

    # ── Шаг 1: Загрузка аккаунта ──
    try:
        async with Session() as db:
            acc = (await db.execute(
                select(TelegramAccount).options(joinedload(TelegramAccount.api_app))
                .where(TelegramAccount.id == account_id)
            )).scalar_one_or_none()
            if not acc:
                result["steps"].append({"step": "load_account", "ok": False, "error": f"#{account_id} нет в БД"})
                return result
            proxy = None
            if acc.proxy_id:
                proxy = (await db.execute(select(Proxy).where(Proxy.id == acc.proxy_id))).scalar_one_or_none()
            phone = acc.phone
            status = acc.status
        result["phone"] = phone
        proxy_str = f"{proxy.host}:{proxy.port}" if proxy else "нет"
        result["steps"].append({"step": "load_account", "ok": True, "detail": f"{phone} status={status} proxy={proxy_str}"})
    except Exception as e:
        result["steps"].append({"step": "load_account", "ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"})
        return result

    # ── Шаг 2: Account lock ──
    if not acquire_account_lock(account_id, ttl=300):
        result["steps"].append({"step": "lock", "ok": False, "error": "другой воркер держит лок"})
        return result
    result["steps"].append({"step": "lock", "ok": True})

    try:
        # ── Шаг 3: Telethon client ──
        client = make_telethon_client(acc, proxy)
        if not client:
            result["steps"].append({"step": "client", "ok": False, "error": "нет session-файла"})
            return result

        try:
            await client.connect()
            if not await client.is_user_authorized():
                result["steps"].append({"step": "connect", "ok": False, "error": "not authorized"})
                return result
            result["steps"].append({"step": "connect", "ok": True})

            # ── Шаг 4: Wait (имитация запланированной сессии) ──
            if wait_seconds > 0:
                print(f"  {C.B}ℹ{C.OFF} [{phone}] ⏱ ждём {wait_seconds}с (имитация connect_at_hour из плана)...")
                await asyncio.sleep(wait_seconds)
                result["steps"].append({"step": "wait", "ok": True, "detail": f"{wait_seconds}с"})

            # ── Шаг 5: JOIN канала (с верификацией) ──
            from telethon.tl.functions.contacts import ResolveUsernameRequest
            from telethon.tl.functions.channels import JoinChannelRequest, GetParticipantRequest
            from telethon.errors import (
                UserAlreadyParticipantError, FloodWaitError,
                ChannelPrivateError, UserBannedInChannelError,
                UsernameNotOccupiedError, UsernameInvalidError,
                InviteRequestSentError, ChannelsTooMuchError,
            )

            # 5a. Resolve
            try:
                resolved = await client(ResolveUsernameRequest(clean_ch))
                if not resolved.chats:
                    result["steps"].append({"step": "resolve", "ok": False, "error": "канал не найден"})
                    return result
                entity = resolved.chats[0]
                ch_title = getattr(entity, 'title', '?')
                result["steps"].append({"step": "resolve", "ok": True, "detail": f"@{clean_ch} → «{ch_title}»"})
            except (UsernameNotOccupiedError, UsernameInvalidError):
                result["steps"].append({"step": "resolve", "ok": False, "error": "невалидный username", "error_type": "UsernameInvalid"})
                return result
            except FloodWaitError as e:
                result["steps"].append({"step": "resolve", "ok": False, "error": f"FloodWait {e.seconds}с"})
                return result
            except Exception as e:
                result["steps"].append({"step": "resolve", "ok": False, "error": f"{type(e).__name__}: {str(e)[:150]}"})
                return result

            me = await client.get_me()

            # 5b. Pre-check
            already_in = False
            try:
                await client(GetParticipantRequest(channel=entity, participant=me))
                already_in = True
                result["steps"].append({"step": "join", "ok": True, "detail": "аккаунт УЖЕ подписан"})
            except Exception:
                pass

            # 5c. Join + verify
            if not already_in:
                try:
                    await client(JoinChannelRequest(entity))
                    await asyncio.sleep(random.uniform(2, 4))
                    await client(GetParticipantRequest(channel=entity, participant=me))
                    result["steps"].append({"step": "join", "ok": True, "detail": "вступил + подтверждено"})
                except UserAlreadyParticipantError:
                    result["steps"].append({"step": "join", "ok": True, "detail": "уже подписан (через Join)"})
                except InviteRequestSentError:
                    result["steps"].append({"step": "join", "ok": False, "error": "канал требует одобрения", "error_type": "InviteRequestSent"})
                    return result
                except ChannelsTooMuchError:
                    result["steps"].append({"step": "join", "ok": False, "error": "лимит 500 каналов", "error_type": "ChannelsTooMuch"})
                    return result
                except (ChannelPrivateError, UserBannedInChannelError) as e:
                    result["steps"].append({"step": "join", "ok": False, "error": f"{type(e).__name__}"})
                    return result
                except FloodWaitError as e:
                    result["steps"].append({"step": "join", "ok": False, "error": f"FloodWait {e.seconds}с"})
                    return result
                except Exception as e:
                    result["steps"].append({"step": "join", "ok": False, "error": f"{type(e).__name__}: {str(e)[:150]}"})
                    return result

            await asyncio.sleep(random.uniform(2, 5))

            # ── Шаг 6: read_feed ──
            try:
                msgs = await client.get_messages(entity, limit=3)
                for m in msgs:
                    await client.send_read_acknowledge(entity, m)
                    await asyncio.sleep(random.uniform(0.4, 1.2))
                result["steps"].append({"step": "read_feed", "ok": True, "detail": f"прочитал {len(msgs)} последних постов"})
            except Exception as e:
                result["steps"].append({"step": "read_feed", "ok": False, "error": f"{type(e).__name__}: {str(e)[:150]}"})

            await asyncio.sleep(random.uniform(2, 4))

            # ── Шаг 7: set_reaction на последний пост ──
            try:
                from telethon.tl.functions.messages import SendReactionRequest
                from telethon.tl.functions.channels import GetFullChannelRequest
                from telethon.tl.types import ReactionEmoji
                from telethon.errors import ReactionInvalidError

                msgs = await client.get_messages(entity, limit=3)
                if not msgs:
                    result["steps"].append({"step": "set_reaction", "ok": False, "error": "нет постов в канале"})
                else:
                    # Разбираем available_reactions канала. Тип может быть:
                    #   ChatReactionsNone  — реакции запрещены
                    #   ChatReactionsAll   — любые стандартные эмодзи
                    #   ChatReactionsSome  — только из whitelist (reactions=[ReactionEmoji, ...])
                    #   None               — старый канал без явных настроек (обычно работает all)
                    allowed = []
                    reaction_mode = "unknown"
                    try:
                        full = await client(GetFullChannelRequest(entity))
                        available = getattr(full.full_chat, 'available_reactions', None)
                        cls_name = type(available).__name__ if available is not None else "None"
                        reaction_mode = cls_name
                        if cls_name == "ChatReactionsSome":
                            for r in getattr(available, 'reactions', []):
                                if hasattr(r, 'emoticon'):
                                    allowed.append(r.emoticon)
                        elif cls_name in ("ChatReactionsAll", "NoneType") or available is None:
                            # All / legacy → разрешён весь стандартный набор
                            allowed = ["👍", "👎", "❤️", "🔥", "🥰", "👏", "😁", "🤔",
                                       "🤯", "😱", "🤬", "😢", "🎉", "🤩", "🙏", "💯"]
                        # ChatReactionsNone → allowed остаётся []
                    except Exception as e:
                        result["steps"].append({"step": "set_reaction", "ok": False,
                                                "error": f"GetFullChannel: {type(e).__name__}"})
                        raise

                    if not allowed:
                        result["steps"].append({"step": "set_reaction", "ok": False,
                                                "error": f"канал не разрешает реакции (mode={reaction_mode})"})
                    else:
                        target_post = msgs[0]
                        # Пытаемся до 3-х разных эмодзи. Если канал отверг 🔥 — попробуем 👍 и т.д.
                        candidates = random.sample(allowed, min(3, len(allowed)))
                        last_err = None
                        success = False
                        for emoji in candidates:
                            try:
                                await client(SendReactionRequest(
                                    peer=entity, msg_id=target_post.id,
                                    reaction=[ReactionEmoji(emoticon=emoji)],
                                ))
                                result["steps"].append({"step": "set_reaction", "ok": True,
                                                        "detail": f"{emoji} на пост #{target_post.id} (mode={reaction_mode})"})
                                success = True
                                break
                            except ReactionInvalidError as e:
                                last_err = e
                                continue
                        if not success:
                            result["steps"].append({"step": "set_reaction", "ok": False,
                                                    "error": f"все {len(candidates)} эмодзи отвергнуты (mode={reaction_mode}): {str(last_err)[:80]}"})
            except Exception as e:
                # Уже залогировано в внутреннем except (GetFullChannel) — здесь не дублируем
                if not any(s["step"] == "set_reaction" for s in result["steps"]):
                    result["steps"].append({"step": "set_reaction", "ok": False,
                                            "error": f"{type(e).__name__}: {str(e)[:150]}"})

            # ── Шаг 8: Leave (опционально, только если только что вступили) ──
            if leave_after and not already_in:
                try:
                    from telethon.tl.functions.channels import LeaveChannelRequest
                    await asyncio.sleep(random.uniform(1, 3))
                    await client(LeaveChannelRequest(entity))
                    result["steps"].append({"step": "leave", "ok": True, "detail": "вышел из канала (--leave)"})
                except Exception as e:
                    result["steps"].append({"step": "leave", "ok": False, "error": f"{type(e).__name__}: {str(e)[:150]}"})

            result["ok"] = True

        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
    finally:
        release_account_lock(account_id)

    result["elapsed"] = round(time.time() - started, 2)
    return result


# ─────────────────────────────────────────────────────────
# Утилиты
# ─────────────────────────────────────────────────────────
def print_result(r, label):
    print(f"\n{C.C}{C.BOLD}── {label} (id={r['account_id']}, {r.get('phone', '?')}) ──{C.OFF}")
    for s in r["steps"]:
        if s["ok"]:
            step_ok(s["step"], s.get("detail", ""))
        else:
            step_fail(s["step"], s.get("error", ""), s.get("error_type", ""))
    if r["ok"]:
        print(f"  {C.G}{C.BOLD}РЕЗУЛЬТАТ: ОК{C.OFF}{C.DIM} ({r.get('elapsed', '?')}с){C.OFF}")
    else:
        last_err = next((s.get("error", "?") for s in reversed(r["steps"]) if not s["ok"]), "?")
        print(f"  {C.R}{C.BOLD}РЕЗУЛЬТАТ: ПРОВАЛ{C.OFF}{C.DIM} — {last_err}{C.OFF}")


async def get_default_accounts(n=2):
    from sqlalchemy import select
    from utils.db_pool import async_session as Session
    from models.account import TelegramAccount
    async with Session() as db:
        result = await db.execute(
            select(TelegramAccount.id)
            .where(TelegramAccount.status.in_(("active", "unknown")))
            .limit(n)
        )
        return [r[0] for r in result.all()]


async def main(args):
    header(f"МИНИ-ПЛАН: connect → wait {args.wait}с → join @{args.channel.lstrip('@')} → read_feed → set_reaction" +
            (" → leave" if args.leave else ""))

    if args.account_ids:
        account_ids = args.account_ids
    else:
        try:
            account_ids = await get_default_accounts(2)
        except Exception as e:
            print(f"{C.R}Не удалось загрузить акки:{C.OFF} {e}")
            return 1

    if not account_ids:
        print(f"{C.R}Нет активных аккаунтов в БД{C.OFF}")
        return 1

    print(f"{C.C}Аккаунты:{C.OFF} {account_ids}")
    print(f"{C.C}Канал:{C.OFF}    @{args.channel.lstrip('@')}")
    print(f"{C.C}Wait:{C.OFF}     {args.wait}с")
    print(f"{C.C}Leave:{C.OFF}    {'да' if args.leave else 'нет (останется подписанным)'}")
    print(f"{C.C}Режим:{C.OFF}    {'ПАРАЛЛЕЛЬНО (gather)' if not args.serial else 'ПОСЛЕДОВАТЕЛЬНО'}")

    started = time.time()

    if args.serial:
        results = []
        for aid in account_ids:
            r = await run_mini_plan(aid, args.channel, args.wait, args.leave)
            results.append(r)
    else:
        results = await asyncio.gather(
            *(run_mini_plan(aid, args.channel, args.wait, args.leave) for aid in account_ids),
            return_exceptions=True,
        )

    # Вывод
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            print(f"\n{C.R}{C.BOLD}── Аккаунт #{account_ids[i]} — UNCAUGHT EXCEPTION ──{C.OFF}")
            print(f"  {type(r).__name__}: {str(r)[:300]}")
        else:
            print_result(r, f"Аккаунт #{i+1}")

    # Итог
    header("ИТОГ")
    total = len(results)
    ok = sum(1 for r in results if isinstance(r, dict) and r.get("ok"))
    elapsed = time.time() - started
    color = C.G if ok == total else (C.Y if ok > 0 else C.R)
    print(f"  {color}{C.BOLD}{ok}/{total} аккаунтов OK{C.OFF}{C.DIM} за {elapsed:.2f}с{C.OFF}")

    if not args.serial and ok >= 2:
        max_indiv = max((r["elapsed"] for r in results if isinstance(r, dict) and r.get("elapsed")), default=0)
        if max_indiv > 0:
            if elapsed < max_indiv * 1.4:
                print(f"  {C.G}✓ Реальный параллелизм{C.OFF}{C.DIM} (max акк: {max_indiv:.2f}с, total: {elapsed:.2f}с){C.OFF}")
            else:
                print(f"  {C.Y}⚠ Сериализация{C.OFF}{C.DIM} (max акк: {max_indiv:.2f}с, total: {elapsed:.2f}с) — что-то блокирует{C.OFF}")

    return 0 if ok == total else 1


def cli():
    parser = argparse.ArgumentParser(description="Прогон мини-плана: connect → wait → join → read_feed → set_reaction")
    parser.add_argument("channel", type=str, help="канал для теста (@durov или durov)")
    parser.add_argument("account_ids", type=int, nargs="*", help="ID аккаунтов (по умолчанию — первые 2 активных)")
    parser.add_argument("--wait", type=int, default=10, help="задержка перед действиями (default: 10с)")
    parser.add_argument("--leave", action="store_true", help="выйти из канала после теста")
    parser.add_argument("--serial", action="store_true", help="последовательно (для сравнения с параллельным)")
    args = parser.parse_args()

    rc = asyncio.run(main(args))
    sys.exit(rc)


if __name__ == "__main__":
    cli()
