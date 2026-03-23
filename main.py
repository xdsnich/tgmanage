"""
GramGPT — main.py
Точка входа. Только логика меню.

Запуск:
  pip install -r requirements.txt
  python main.py
"""

import asyncio
import json
import sys
from pathlib import Path

try:
    from telethon import TelegramClient
except ImportError:
    print("❌ Установи зависимости: pip install -r requirements.txt")
    sys.exit(1)

import config
import ui
import tg_client
import proxy_manager as pm
import profile_manager as pm_profile
import actions as act
import security as sec
import channel_manager as ch
import analytics
import tdata_importer as tdata
from db import (
    load_accounts, save_accounts, upsert_account,
    find_account, find_account_index,
    load_proxies, save_proxies,
    parse_proxy_line,
)
import trust as trust_module


# ============================================================
# ОБЩИЙ ВЫБОР ПУЛА АККАУНТОВ
# ============================================================

def pick_accounts(accounts: list) -> list:
    print("\n  Применить к:")
    print("  1 — Всем аккаунтам")
    print("  2 — Конкретному аккаунту")
    pick = input("  Выбор [1]: ").strip() or "1"
    if pick == "2":
        for i, a in enumerate(accounts):
            print(f"  {i+1}. {a['phone']} ({a.get('first_name','')}) [{a.get('status','?')}]")
        idx = input("  Номер аккаунта (1, 2, ...): ").strip()
        if not idx:
            ui.warn("Ничего не введено — выбраны все аккаунты")
            return accounts
        try:
            chosen = int(idx) - 1
            if chosen < 0 or chosen >= len(accounts):
                ui.err(f"Нет аккаунта с номером {idx}. Доступно: 1–{len(accounts)}")
                return []
            return [accounts[chosen]]
        except ValueError:
            ui.err(f"'{idx}' — не число")
            return []
    return accounts


# ============================================================
# АККАУНТЫ
# ============================================================

async def action_add_account(accounts: list) -> list:
    ui.divider("Добавить аккаунт")
    phone = input("  Номер телефона (+79991234567): ").strip()
    if not phone.startswith("+"):
        phone = "+" + phone

    existing = find_account(accounts, phone)
    if existing:
        ui.warn(f"Аккаунт {phone} уже добавлен.")
        if input("  Перепроверить? (y/n): ").strip().lower() == "y":
            updated = await tg_client.check(existing, check_spam=False)
            accounts = upsert_account(accounts, updated)
            ui.account_card(updated, find_account_index(accounts, phone))
        return accounts

    account = await tg_client.authorize(phone)
    if account.get("status") == "error":
        ui.err(f"Ошибка: {account.get('error')}")
    else:
        accounts = upsert_account(accounts, account)
        ui.ok("Аккаунт успешно добавлен!")
        ui.account_card(account, find_account_index(accounts, phone))

    return accounts


async def action_check_all(accounts: list) -> list:
    if not accounts:
        ui.warn("Нет аккаунтов.")
        return accounts

    ui.divider("Проверка аккаунтов")
    targets = pick_accounts(accounts)
    if not targets:
        return accounts

    check_spam = input("\n  Проверять спамблок через @SpamBot? (y/n) [n]: ").strip().lower() == "y"
    if check_spam:
        ui.warn("Проверка спамблока — ~15 сек на аккаунт")

    for i, account in enumerate(targets):
        print(f"\n  [{i+1}/{len(targets)}] {account.get('phone')}")
        updated = await tg_client.check(account, check_spam=check_spam)
        idx = find_account_index(accounts, account["phone"])
        if idx >= 0:
            accounts[idx] = updated
        ui.account_card(updated, idx)

    save_accounts(accounts)
    ui.ok("Проверка завершена.")
    return accounts


def action_list_accounts(accounts: list):
    if not accounts:
        ui.warn("Нет аккаунтов.")
        return
    ui.divider("Все аккаунты")
    ui.accounts_summary(accounts)
    for i, acc in enumerate(accounts):
        ui.account_card(acc, i)


def action_export(accounts: list):
    if not accounts:
        ui.warn("Нет данных для экспорта.")
        return
    path = config.DATA_DIR / "accounts_export.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(accounts, f, ensure_ascii=False, indent=2, default=str)
    ui.ok(f"Экспортировано {len(accounts)} аккаунтов → {path}")


# ============================================================
# TDATA ИМПОРТ (v0.6A)
# ============================================================

async def action_tdata_menu(accounts: list) -> list:
    while True:
        ui.tdata_menu()
        choice = input("  Выбор: ").strip()

        if choice == "0":
            break

        elif choice == "1":
            ui.divider("Импорт TData")
            path = input("  Путь к папке TData: ").strip().strip('"')
            if not path:
                continue
            phone = input("  Номер телефона (если знаешь, Enter — пропустить): ").strip()
            account = await tdata.import_tdata(path, phone)
            if account:
                accounts = upsert_account(accounts, account)
                save_accounts(accounts)
                ui.account_card(account, find_account_index(accounts, account["phone"]))

        elif choice == "2":
            ui.divider("Пакетный импорт TData")
            ui.info("Введи пути к папкам TData — по одному на строку.")
            ui.info("Пустая строка — завершить ввод.")
            paths = []
            while True:
                p = input(f"  Папка {len(paths)+1}: ").strip().strip('"')
                if not p:
                    break
                paths.append(p)
            if paths:
                accounts = await tdata.batch_import_tdata(paths, accounts)

        else:
            ui.err("Неверный выбор")

    return accounts


# ============================================================
# ПРОФИЛИ (v0.3)
# ============================================================

async def action_profile_menu(accounts: list) -> list:
    if not accounts:
        ui.warn("Нет аккаунтов.")
        return accounts

    while True:
        ui.profile_menu(len(accounts))
        choice = input("  Выбор: ").strip()

        if choice == "0":
            break
        elif choice == "1":
            targets = pick_accounts(accounts)
            if not targets:
                continue
            first = input("  Имя (Enter — без изменений): ").strip() or None
            last  = input("  Фамилия (Enter — без изменений): ").strip() or None
            if not first and not last:
                continue
            updated = await pm_profile.batch_update_profile(targets, first_name=first, last_name=last)
            for u in updated:
                accounts = upsert_account(accounts, u)
            save_accounts(accounts)

        elif choice == "2":
            targets = pick_accounts(accounts)
            if not targets:
                continue
            bio = input("  Bio (до 70 символов): ").strip()
            if not bio:
                continue
            updated = await pm_profile.batch_update_profile(targets, bio=bio[:70])
            for u in updated:
                accounts = upsert_account(accounts, u)
            save_accounts(accounts)

        elif choice == "3":
            targets = pick_accounts(accounts)
            if not targets:
                continue
            path = input("  Путь к файлу JPG/PNG: ").strip().strip('"')
            if not path:
                continue
            updated = await pm_profile.batch_set_avatar(targets, path)
            for u in updated:
                idx = find_account_index(accounts, u["phone"])
                if idx >= 0:
                    accounts[idx]["has_photo"] = u.get("has_photo", False)
                    accounts[idx]["trust_score"] = trust_module.calculate(accounts[idx])
            save_accounts(accounts)

        elif choice == "4":
            for i, a in enumerate(accounts):
                print(f"  {i+1}. {a['phone']}  [{', '.join(a.get('tags',[]) or ['—'])}]")
            idx = input("\n  Номер аккаунта: ").strip()
            try:
                acc = accounts[int(idx) - 1]
            except Exception:
                ui.err("Неверный номер")
                continue
            print("  1 — Добавить  2 — Удалить")
            act_t = input("  Действие: ").strip()
            tag = input("  Тег: ").strip()
            if not tag:
                continue
            if act_t == "1":
                acc = pm_profile.set_tag(acc, tag)
            elif act_t == "2":
                acc = pm_profile.remove_tag(acc, tag)
            accounts = upsert_account(accounts, acc)
            save_accounts(accounts)

        elif choice == "5":
            roles = pm_profile.VALID_ROLES
            for i, r in enumerate(roles):
                print(f"  {i+1}. {r}")
            for i, a in enumerate(accounts):
                print(f"\n  {a['phone']} — сейчас: {a.get('role','default')}")
                r_idx = input("  Новая роль (Enter — пропустить): ").strip()
                if r_idx:
                    try:
                        accounts[i] = pm_profile.set_role(accounts[i], roles[int(r_idx)-1])
                    except Exception:
                        ui.err("Неверный номер")
            save_accounts(accounts)

        elif choice == "6":
            for i, a in enumerate(accounts):
                print(f"  {i+1}. {a['phone']}  {a.get('notes') or '—'}")
            idx = input("\n  Номер аккаунта: ").strip()
            try:
                acc = accounts[int(idx) - 1]
            except Exception:
                ui.err("Неверный номер")
                continue
            note = input("  Заметка: ").strip()
            acc = pm_profile.set_note(acc, note)
            accounts = upsert_account(accounts, acc)
            save_accounts(accounts)

        else:
            ui.err("Неверный выбор")

    return accounts


# ============================================================
# КАНАЛЫ (v0.6A)
# ============================================================

async def action_channels_menu(accounts: list) -> list:
    if not accounts:
        ui.warn("Нет аккаунтов.")
        return accounts

    while True:
        ui.channels_menu()
        choice = input("  Выбор: ").strip()

        if choice == "0":
            break

        elif choice == "1":
            ui.divider("Мои каналы")
            targets = pick_accounts(accounts)
            if not targets:
                continue
            for account in targets:
                print(f"\n  {account['phone']}:")
                channels = await ch.get_my_channels(account)
                if not channels:
                    print("  Нет каналов")
                else:
                    for c in channels:
                        print(f"  📢 {c['title']}  {c['link']}  ({c.get('members',0)} подписчиков)")
                # Сохраняем найденные каналы в аккаунт
                if channels:
                    idx = find_account_index(accounts, account["phone"])
                    accounts[idx]["channels"] = channels
            save_accounts(accounts)

        elif choice == "2":
            ui.divider("Создать канал")
            targets = pick_accounts(accounts)
            if not targets:
                continue
            title = input("  Название канала: ").strip()
            if not title:
                continue
            desc  = input("  Описание (Enter — пропустить): ").strip()
            uname = input("  Username (Enter — без username): ").strip()
            for account in targets:
                channel = await ch.create_channel(account, title, desc, uname)
                if channel:
                    idx = find_account_index(accounts, account["phone"])
                    accounts[idx].setdefault("channels", []).append(channel)
            save_accounts(accounts)

        elif choice == "3":
            ui.divider("Создать каналы пакетно")
            targets = pick_accounts(accounts)
            if not targets:
                continue
            ui.info("Используй {name} для имени аккаунта, {n} для номера")
            template = input("  Шаблон названия (напр. 'Канал {name}'): ").strip()
            if not template:
                continue
            desc = input("  Описание (Enter — пропустить): ").strip()
            updated = await ch.batch_create_channels(targets, template, desc)
            for u in updated:
                accounts = upsert_account(accounts, u)
            save_accounts(accounts)

        elif choice == "4":
            ui.divider("Закрепить канал в профиле")
            targets = pick_accounts(accounts)
            if not targets:
                continue

            for account in targets:
                phone = account["phone"]
                channels = account.get("channels", [])

                # Фильтруем мусорные id-ссылки
                valid_channels = [
                    c for c in channels
                    if c.get("link") and not c["link"].startswith("id")
                ]

                if not valid_channels:
                    ui.warn(f"[{phone}] Нет сохранённых каналов.")
                    ui.info(f"[{phone}] Сначала запусти пункт 1 (Мои каналы) или пункт 5 (Закрепить существующий)")
                    continue

                # Показываем список каналов для выбора
                print(f"\n  Каналы аккаунта {phone}:")
                for i, c in enumerate(valid_channels):
                    print(f"  {i+1}. {c.get('title','?')}  {c.get('link','')}")
                idx = input("  Выбери канал (номер): ").strip()
                try:
                    chosen = valid_channels[int(idx) - 1]
                except Exception:
                    ui.err("Неверный номер")
                    continue

                idx_acc = find_account_index(accounts, phone)
                await ch.pin_channel_to_profile(accounts[idx_acc], chosen["link"])

            save_accounts(accounts)

        elif choice == "5":
            ui.divider("Закрепить существующий канал")
            targets = pick_accounts(accounts)
            if not targets:
                continue
            link = input("  Ссылка или @username канала: ").strip()
            if not link:
                continue
            for account in targets:
                idx = find_account_index(accounts, account["phone"])
                ok = await ch.pin_existing_channel(accounts[idx], link)
                if ok:
                    save_accounts(accounts)

        else:
            ui.err("Неверный выбор")

    return accounts


# ============================================================
# БЫСТРЫЕ ДЕЙСТВИЯ (v0.4)
# ============================================================

async def action_actions_menu(accounts: list) -> list:
    if not accounts:
        ui.warn("Нет аккаунтов.")
        return accounts

    while True:
        ui.actions_menu(len(accounts))
        choice = input("  Выбор: ").strip()

        if choice == "0":
            break

        targets = pick_accounts(accounts)
        if not targets:
            continue

        if choice in ["1", "2", "3"]:
            ui.warn("Необратимое действие!")
            if input("  Продолжить? (y/n): ").strip().lower() != "y":
                continue

        if choice == "1":
            for a in targets:
                accounts[find_account_index(accounts, a["phone"])] = await act.leave_all_chats(a)
            save_accounts(accounts)
        elif choice == "2":
            for a in targets:
                accounts[find_account_index(accounts, a["phone"])] = await act.leave_all_channels(a)
            save_accounts(accounts)
        elif choice == "3":
            for a in targets:
                accounts[find_account_index(accounts, a["phone"])] = await act.delete_private_chats(a)
            save_accounts(accounts)
        elif choice == "4":
            for a in targets:
                accounts[find_account_index(accounts, a["phone"])] = await act.read_all_messages(a)
            save_accounts(accounts)
        elif choice == "5":
            for a in targets:
                accounts[find_account_index(accounts, a["phone"])] = await act.unpin_folders(a)
            save_accounts(accounts)
        elif choice == "6":
            quarantined = [a for a in accounts if a.get("status") == "quarantine"]
            if not quarantined:
                ui.info("Нет аккаунтов в карантине")
            else:
                for i, a in enumerate(quarantined):
                    print(f"  {i+1}. {a['phone']} — {a.get('quarantine_reason','?')}")
                idx = input("\n  Номер для снятия (Enter — пропустить): ").strip()
                if idx:
                    try:
                        acc = act.lift_quarantine(quarantined[int(idx) - 1])
                        accounts = upsert_account(accounts, acc)
                        save_accounts(accounts)
                    except Exception:
                        ui.err("Неверный номер")
        else:
            ui.err("Неверный выбор")

    return accounts


# ============================================================
# БЕЗОПАСНОСТЬ (v0.5)
# ============================================================

async def action_security_menu(accounts: list) -> list:
    if not accounts:
        ui.warn("Нет аккаунтов.")
        return accounts

    while True:
        ui.security_menu()
        choice = input("  Выбор: ").strip()

        if choice == "0":
            break

        elif choice == "1":
            targets = pick_accounts(accounts)
            if not targets:
                continue
            for account in targets:
                sessions = await sec.list_sessions(account)
                ui.print_sessions(sessions, account["phone"])

        elif choice == "2":
            targets = pick_accounts(accounts)
            if not targets:
                continue
            for account in targets:
                await sec.get_auth_code(account)

        elif choice == "3":
            targets = pick_accounts(accounts)
            if not targets:
                continue
            await sec.export_sessions_json(targets)

        elif choice == "4":
            ui.warn("Все устройства кроме текущего будут отключены!")
            if input("  Введи CONFIRM: ").strip() != "CONFIRM":
                ui.info("Отменено.")
                continue
            targets = pick_accounts(accounts)
            if not targets:
                continue
            for account in targets:
                idx = find_account_index(accounts, account["phone"])
                accounts[idx] = await sec.terminate_other_sessions(account)
            save_accounts(accounts)

        elif choice == "5":
            ui.warn("Все сессии будут сброшены. Потребуется новый SMS-код.")
            if input("  Введи CONFIRM: ").strip() != "CONFIRM":
                ui.info("Отменено.")
                continue
            targets = pick_accounts(accounts)
            if not targets:
                continue
            for account in targets:
                updated = await sec.reauthorize(account)
                accounts = upsert_account(accounts, updated)
            save_accounts(accounts)

        elif choice == "6":
            ui.warn("Это действие по ручному запросу.")
            targets = pick_accounts(accounts)
            if not targets:
                continue
            password = input("  Пароль 2FA (мин. 6 символов): ").strip()
            if len(password) < 6:
                ui.err("Слишком короткий пароль")
                continue
            if input("  Повтори пароль: ").strip() != password:
                ui.err("Пароли не совпадают")
                continue
            hint = input("  Подсказка (Enter — без): ").strip()
            updated = await sec.batch_set_2fa(targets, password, hint)
            for u in updated:
                accounts = upsert_account(accounts, u)
            save_accounts(accounts)

        else:
            ui.err("Неверный выбор")

    return accounts


# ============================================================
# АНАЛИТИКА (v0.6A)
# ============================================================

async def action_analytics_menu(accounts: list):
    if not accounts:
        ui.warn("Нет аккаунтов.")
        return

    while True:
        ui.analytics_menu()
        choice = input("  Выбор: ").strip()

        if choice == "0":
            break

        elif choice == "1":
            analytics.health_dashboard(accounts)

        elif choice == "2":
            for i, a in enumerate(accounts):
                print(f"  {i+1}. {a['phone']} ({a.get('first_name','')}) [{a.get('status','?')}] Trust:{a.get('trust_score',0)}")
            idx = input("\n  Номер аккаунта: ").strip()
            try:
                analytics.account_detail(accounts[int(idx) - 1])
            except Exception:
                ui.err("Неверный номер")

        elif choice == "3":
            query = input("  Поиск (номер / username / имя / тег / статус): ").strip()
            if not query:
                continue
            results = analytics.search_accounts(accounts, query)
            ui.divider(f"Результаты поиска: {len(results)}")
            for i, a in enumerate(results):
                ui.account_card(a, i)

        elif choice == "4":
            ui.divider("Фильтрация")
            print("  Статус (active/spamblock/frozen/quarantine/Enter — все): ", end="")
            status = input().strip() or None
            print("  Мин. Trust Score (Enter — пропустить): ", end="")
            min_t = input().strip()
            min_trust = int(min_t) if min_t.isdigit() else None
            print("  Роль (Enter — все): ", end="")
            role = input().strip() or None

            results = analytics.filter_accounts(
                accounts, status=status, role=role, min_trust=min_trust
            )
            # Сортировка по Trust Score
            results = analytics.sort_accounts(results, by="trust")
            ui.divider(f"Найдено: {len(results)}")
            for i, a in enumerate(results):
                ui.account_card(a, i)

        else:
            ui.err("Неверный выбор")


# ============================================================
# ПРОКСИ
# ============================================================

async def action_proxy_menu(accounts: list) -> list:
    proxies = load_proxies()

    while True:
        ui.proxy_menu()
        choice = input("  Выбор: ").strip()

        if choice == "1":
            if not proxies:
                ui.warn("Нет прокси.")
            else:
                valid     = sum(1 for p in proxies if p.get("is_valid") is True)
                invalid   = sum(1 for p in proxies if p.get("is_valid") is False)
                unchecked = len(proxies) - valid - invalid
                print(f"\n  Всего: {len(proxies)}  ✅ {valid}  ❌ {invalid}  ❓ {unchecked}\n")
                for i, proxy in enumerate(proxies):
                    ui.proxy_row(proxy, i)

        elif choice == "2":
            line = input("  Прокси (host:port:login:pass): ").strip()
            proxy = parse_proxy_line(line)
            if proxy:
                proxies.append(proxy)
                save_proxies(proxies)
                ui.ok(f"Добавлен: {proxy['id']}")
            else:
                ui.err("Не удалось распознать формат")

        elif choice == "3":
            filepath = input("  Файл (Enter = proxies.txt): ").strip() or "proxies.txt"
            new_proxies = pm.load_from_file(filepath)
            if new_proxies:
                existing_ids = {p["id"] for p in proxies}
                new_only = [p for p in new_proxies if p["id"] not in existing_ids]
                proxies += new_only
                save_proxies(proxies)
                ui.ok(f"Добавлено {len(new_only)} прокси")

        elif choice == "4":
            if proxies:
                proxies = await pm.check_all(proxies)
                save_proxies(proxies)
                valid = sum(1 for p in proxies if p.get("is_valid"))
                ui.ok(f"Валидных: {valid}/{len(proxies)}")

        elif choice == "5":
            if accounts and proxies:
                mode = "random" if input("  1-порядок / 2-случайно [1]: ").strip() == "2" else "sequential"
                accounts, proxies = pm.assign_proxies(accounts, proxies, mode)
                save_accounts(accounts)
                save_proxies(proxies)

        elif choice == "0":
            break
        else:
            ui.err("Неверный выбор")

    return accounts


# ============================================================
# ГЛАВНЫЙ ЦИКЛ
# ============================================================

async def main():
    ui.banner()

    if not config.API_ID or not config.API_HASH:
        ui.warn("Заполни TG_API_ID и TG_API_HASH в файле .env")
        ui.info("Ключи: https://my.telegram.org")

    accounts = load_accounts()
    ui.info(f"Загружено аккаунтов: {len(accounts)}")

    # Валидация путей сессий при старте
    broken = []
    for acc in accounts:
        sf = acc.get("session_file", "")
        if sf and not Path(sf).exists():
            broken.append((acc["phone"], sf))
    if broken:
        ui.warn(f"Найдено {len(broken)} аккаунтов с битыми путями к сессиям:")
        for phone, path in broken:
            print(f"    ❌ {phone}: {path}")
        ui.info("Исправь пути в data/accounts.json или переавторизуй (пункт 1)")

    while True:
        ui.main_menu()
        choice = input("  Выбор: ").strip()

        if   choice == "1": accounts = await action_add_account(accounts)
        elif choice == "2": accounts = await action_tdata_menu(accounts)
        elif choice == "3": action_list_accounts(accounts)
        elif choice == "4": accounts = await action_check_all(accounts)
        elif choice == "5": accounts = await action_profile_menu(accounts)
        elif choice == "6": accounts = await action_channels_menu(accounts)
        elif choice == "7": accounts = await action_actions_menu(accounts)
        elif choice == "8": accounts = await action_security_menu(accounts)
        elif choice == "9": await action_analytics_menu(accounts)
        elif choice == "p": accounts = await action_proxy_menu(accounts)
        elif choice == "e": action_export(accounts)
        elif choice == "0":
            print("\n  До встречи! 👋\n")
            break
        else:
            ui.err("Неверный выбор")


if __name__ == "__main__":
    asyncio.run(main())