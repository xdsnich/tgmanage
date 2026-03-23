"""
GramGPT — ui.py
Весь вывод в терминал
Отвечает за: цвета, карточки аккаунтов, баннер, меню, таблицы
"""

try:
    from colorama import init, Fore, Style
    init(autoreset=True)
except ImportError:
    class Fore:
        RED = GREEN = YELLOW = CYAN = BLUE = MAGENTA = WHITE = ""
    class Style:
        BRIGHT = RESET_ALL = ""

import trust as trust_module


# ============================================================
# БАННЕР
# ============================================================

def banner():
    print(f"""
{Fore.CYAN}{Style.BRIGHT}
  ██████╗ ██████╗  █████╗ ███╗   ███╗ ██████╗ ██████╗ ████████╗
 ██╔════╝ ██╔══██╗██╔══██╗████╗ ████║██╔════╝ ██╔══██╗╚══██╔══╝
 ██║  ███╗██████╔╝███████║██╔████╔██║██║  ███╗██████╔╝   ██║   
 ██║   ██║██╔══██╗██╔══██║██║╚██╔╝██║██║   ██║██╔═══╝    ██║   
 ╚██████╔╝██║  ██║██║  ██║██║ ╚═╝ ██║╚██████╔╝██║        ██║   
  ╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝     ╚═╝ ╚═════╝ ╚═╝        ╚═╝   
{Style.RESET_ALL}
{Fore.WHITE}  Менеджер Telegram-аккаунтов  |  MVP v0.2{Style.RESET_ALL}
{Fore.WHITE}  ──────────────────────────────────────────{Style.RESET_ALL}
""")


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ
# ============================================================

def status_icon(status: str) -> str:
    icons = {
        "active":    f"{Fore.GREEN}✅ Живой{Style.RESET_ALL}",
        "spamblock": f"{Fore.RED}🚫 Спамблок{Style.RESET_ALL}",
        "frozen":    f"{Fore.YELLOW}❄️  Заморожен{Style.RESET_ALL}",
        "error":     f"{Fore.RED}❌ Ошибка{Style.RESET_ALL}",
        "unknown":   f"{Fore.WHITE}❓ Неизвестно{Style.RESET_ALL}",
    }
    return icons.get(status, icons["unknown"])


def trust_bar(score: int) -> str:
    filled = int(score / 10)
    bar = "█" * filled + "░" * (10 - filled)
    color = Fore.GREEN if score >= 70 else (Fore.YELLOW if score >= 40 else Fore.RED)
    grade = trust_module.get_grade(score)
    return f"{color}{bar}{Style.RESET_ALL} {score}/100  ({grade})"


def ok(text: str):
    print(f"{Fore.GREEN}  ✅ {text}{Style.RESET_ALL}")

def warn(text: str):
    print(f"{Fore.YELLOW}  ⚠️  {text}{Style.RESET_ALL}")

def err(text: str):
    print(f"{Fore.RED}  ❌ {text}{Style.RESET_ALL}")

def info(text: str):
    print(f"{Fore.CYAN}  ℹ️  {text}{Style.RESET_ALL}")

def divider(title: str = ""):
    if title:
        print(f"\n{Fore.CYAN}━━ {title} ━━{Style.RESET_ALL}")
    else:
        print(f"{Fore.CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{Style.RESET_ALL}")


# ============================================================
# КАРТОЧКА АККАУНТА
# ============================================================

def account_card(account: dict, index: int):
    name = f"{account.get('first_name','')} {account.get('last_name','')}".strip() or "Без имени"
    username = f"@{account['username']}" if account.get("username") else "нет username"
    phone = account.get("phone", "?")
    status = account.get("status", "unknown")
    score = account.get("trust_score", 0)
    sessions = account.get("active_sessions", "?")
    bio = (account.get("bio") or "—")[:55]
    added = (account.get("added_at") or "?")[:10]
    checked = (account.get("last_checked") or "никогда")[:16].replace("T", " ")
    role = account.get("role", "default")
    tags = ", ".join(account.get("tags", [])) or "—"
    proxy = account.get("proxy") or "нет"

    print(f"""
{Fore.CYAN}  ┌─ #{index + 1} ─────────────────────────────────────────┐{Style.RESET_ALL}
  │  {Style.BRIGHT}{Fore.WHITE}{name}{Style.RESET_ALL}  {Fore.WHITE}({username}){Style.RESET_ALL}
  │  📱 {phone}  │  Роль: {role}
  │  Статус:      {status_icon(status)}
  │  Trust Score: {trust_bar(score)}
  │  Сессии:      {sessions} активных устройств
  │  Bio:         {bio}
  │  Теги:        {tags}
  │  Прокси:      {proxy}
  │  Добавлен:    {added}  │  Проверен: {checked}
{Fore.CYAN}  └────────────────────────────────────────────────┘{Style.RESET_ALL}""")

    # Рекомендации
    tips = trust_module.get_recommendations(account)
    if tips:
        print(f"  {Fore.YELLOW}💡 Рекомендации:{Style.RESET_ALL}")
        for tip in tips:
            print(f"  {Fore.YELLOW}   • {tip}{Style.RESET_ALL}")


# ============================================================
# СТАТИСТИКА ПО ПУЛУ
# ============================================================

def accounts_summary(accounts: list[dict]):
    if not accounts:
        return
    total = len(accounts)
    active = sum(1 for a in accounts if a.get("status") == "active")
    spam = sum(1 for a in accounts if a.get("status") == "spamblock")
    frozen = sum(1 for a in accounts if a.get("status") == "frozen")
    errors = sum(1 for a in accounts if a.get("status") == "error")
    unknown = total - active - spam - frozen - errors
    scores = [a.get("trust_score", 0) for a in accounts]
    avg = sum(scores) // len(scores)

    print(f"""
  {Fore.WHITE}Всего аккаунтов: {Style.BRIGHT}{total}{Style.RESET_ALL}
  {Fore.GREEN}✅ Живых: {active}{Style.RESET_ALL}   {Fore.RED}🚫 Спамблок: {spam}{Style.RESET_ALL}   {Fore.YELLOW}❄️  Заморожено: {frozen}{Style.RESET_ALL}   {Fore.RED}❌ Ошибок: {errors}{Style.RESET_ALL}   {Fore.WHITE}❓ Не проверено: {unknown}{Style.RESET_ALL}
  Средний Trust Score: {trust_bar(avg)}
""")


# ============================================================
# ПРОКСИ
# ============================================================

def proxy_row(proxy: dict, index: int):
    pid = proxy.get("id", "?")
    protocol = proxy.get("protocol", "?").upper()
    assigned = len(proxy.get("assigned_to", []))
    checked = (proxy.get("last_checked") or "никогда")[:16].replace("T", " ")

    valid = proxy.get("is_valid")
    if valid is True:
        status = f"{Fore.GREEN}✅ Валидный{Style.RESET_ALL}"
    elif valid is False:
        status = f"{Fore.RED}❌ Не работает{Style.RESET_ALL}"
    else:
        status = f"{Fore.WHITE}❓ Не проверен{Style.RESET_ALL}"

    print(f"  {index+1:>3}. {Fore.WHITE}{pid:<25}{Style.RESET_ALL}  {protocol:<6}  {status}  Назначен: {assigned} акк.  Проверен: {checked}")


# ============================================================
# ГЛАВНОЕ МЕНЮ
# ============================================================

def main_menu():
    print(f"""
{Fore.CYAN}━━ МЕНЮ ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{Style.RESET_ALL}
  {Fore.WHITE}1{Style.RESET_ALL} — Добавить аккаунт (session)
  {Fore.WHITE}2{Style.RESET_ALL} — Импорт TData
  {Fore.WHITE}3{Style.RESET_ALL} — Список аккаунтов
  {Fore.WHITE}4{Style.RESET_ALL} — Проверить аккаунты
  {Fore.WHITE}5{Style.RESET_ALL} — Управление профилями
  {Fore.WHITE}6{Style.RESET_ALL} — Управление каналами  {Fore.CYAN}← NEW{Style.RESET_ALL}
  {Fore.WHITE}7{Style.RESET_ALL} — Быстрые действия
  {Fore.WHITE}8{Style.RESET_ALL} — Безопасность и сессии
  {Fore.WHITE}9{Style.RESET_ALL} — Аналитика / Dashboard  {Fore.CYAN}← NEW{Style.RESET_ALL}
  {Fore.WHITE}p{Style.RESET_ALL} — Прокси
  {Fore.WHITE}e{Style.RESET_ALL} — Экспорт JSON
  {Fore.WHITE}0{Style.RESET_ALL} — Выход
{Fore.CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{Style.RESET_ALL}""")


def proxy_menu():
    print(f"""
{Fore.CYAN}━━ ПРОКСИ ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{Style.RESET_ALL}
  {Fore.WHITE}1{Style.RESET_ALL} — Список прокси
  {Fore.WHITE}2{Style.RESET_ALL} — Добавить прокси вручную
  {Fore.WHITE}3{Style.RESET_ALL} — Загрузить список из файла (proxies.txt)
  {Fore.WHITE}4{Style.RESET_ALL} — Проверить все прокси
  {Fore.WHITE}5{Style.RESET_ALL} — Назначить прокси на аккаунты
  {Fore.WHITE}0{Style.RESET_ALL} — Назад
{Fore.CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{Style.RESET_ALL}""")


def profile_menu(count: int):
    print(f"""
\033[36m━━ ПРОФИЛИ ({count} акк.) ━━━━━━━━━━━━━━━━━━━━━━━━━━\033[0m
  \033[37m1\033[0m — Изменить имя / фамилию
  \033[37m2\033[0m — Изменить Bio
  \033[37m3\033[0m — Установить аватарку (из файла)
  \033[37m4\033[0m — Управление тегами
  \033[37m5\033[0m — Установить роль
  \033[37m6\033[0m — Добавить заметку
  \033[37m0\033[0m — Назад
\033[36m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\033[0m""")


def actions_menu(count: int):
    print(f"""
\033[36m━━ БЫСТРЫЕ ДЕЙСТВИЯ ({count} акк.) ━━━━━━━━━━━━━━━━━\033[0m
  \033[37m1\033[0m — Выйти из всех чатов
  \033[37m2\033[0m — Отписаться от всех каналов
  \033[37m3\033[0m — Удалить личные переписки
  \033[37m4\033[0m — Прочитать все сообщения
  \033[37m5\033[0m — Открепить папки (высвободить лимиты)
  \033[37m6\033[0m — Карантин (управление)
  \033[37m0\033[0m — Назад
\033[36m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\033[0m""")



def security_menu():
    print(f"""
{Fore.CYAN}━━ БЕЗОПАСНОСТЬ И СЕССИИ ━━━━━━━━━━━━━━━━━━━{Style.RESET_ALL}
  {Fore.WHITE}1{Style.RESET_ALL} — Список активных сессий аккаунта
  {Fore.WHITE}2{Style.RESET_ALL} — Получить код авторизации
  {Fore.WHITE}3{Style.RESET_ALL} — Экспорт сессий в JSON
  {Fore.RED}─────────────────── ОСТОРОЖНО ───────────────{Style.RESET_ALL}
  {Fore.RED}4{Style.RESET_ALL} — Завершить сторонние сессии  {Fore.YELLOW}[ручной запрос]{Style.RESET_ALL}
  {Fore.RED}5{Style.RESET_ALL} — Переавторизация             {Fore.YELLOW}[ручной запрос]{Style.RESET_ALL}
  {Fore.RED}6{Style.RESET_ALL} — Установить 2FA              {Fore.YELLOW}[ручной запрос]{Style.RESET_ALL}
  {Fore.WHITE}0{Style.RESET_ALL} — Назад
{Fore.CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{Style.RESET_ALL}""")


def print_sessions(sessions: list, phone: str):
    if not sessions:
        print(f"  Нет данных о сессиях для {phone}")
        return
    print(f"\n  Активных сессий: {len(sessions)}")
    for s in sessions:
        current = " ← ТЕКУЩАЯ" if s.get("current") else ""
        print(f"""
  {'─'*48}
  📱 {s['app_name']} {s['app_version']}{current}
     Устройство: {s['device_model']} ({s['platform']})
     ОС:         {s['system_version']}
     Регион:     {s.get('country', '?')} / {s.get('region', '?')}
     Активен:    {str(s.get('date_active', '?'))[:16]}""")


def channels_menu():
    print(f"""
{Fore.CYAN}━━ КАНАЛЫ ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{Style.RESET_ALL}
  {Fore.WHITE}1{Style.RESET_ALL} — Мои каналы (список)
  {Fore.WHITE}2{Style.RESET_ALL} — Создать новый канал
  {Fore.WHITE}3{Style.RESET_ALL} — Создать каналы пакетно
  {Fore.WHITE}4{Style.RESET_ALL} — Закрепить канал в профиле (bio)
  {Fore.WHITE}5{Style.RESET_ALL} — Закрепить существующий канал
  {Fore.WHITE}0{Style.RESET_ALL} — Назад
{Fore.CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{Style.RESET_ALL}""")


def analytics_menu():
    print(f"""
{Fore.CYAN}━━ АНАЛИТИКА ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{Style.RESET_ALL}
  {Fore.WHITE}1{Style.RESET_ALL} — Health Dashboard (общая сводка)
  {Fore.WHITE}2{Style.RESET_ALL} — Детальный просмотр аккаунта
  {Fore.WHITE}3{Style.RESET_ALL} — Поиск по аккаунтам
  {Fore.WHITE}4{Style.RESET_ALL} — Фильтрация (статус / Trust / роль)
  {Fore.WHITE}0{Style.RESET_ALL} — Назад
{Fore.CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{Style.RESET_ALL}""")


def tdata_menu():
    print(f"""
{Fore.CYAN}━━ ИМПОРТ TDATA ━━━━━━━━━━━━━━━━━━━━━━━━━━━{Style.RESET_ALL}
  {Fore.WHITE}1{Style.RESET_ALL} — Импорт одной TData папки
  {Fore.WHITE}2{Style.RESET_ALL} — Пакетный импорт (несколько папок)
  {Fore.WHITE}0{Style.RESET_ALL} — Назад
{Fore.CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{Style.RESET_ALL}""")