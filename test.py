from pyrogram import Client

# Твои данные приложения (можно взять с my.telegram.org)
api_id = 21267081
api_hash = "dbac522d32657fbe2f77e280d35564e5"

# Настройки твоего прокси
proxy = {
    "scheme": "socks5",  # Строго socks5!
    "hostname": "170.168.161.81",
    "port": 63253,        # ВНИМАНИЕ: Порт должен быть числом, а не строкой (без кавычек)
    "username": "d9VMTTsk",  # Если прокси без пароля, удали username и password
    "password": "DzQSjAhD"
}

# Инициализируем клиента
# Запускаем БЕЗ proxy=proxy
app = Client("test_session", api_id=api_id, api_hash=api_hash, proxy=proxy)

async def main():
    print("Пытаемся пробиться к дата-центрам Telegram через прокси...")
    try:
        # Пытаемся установить MTProto соединение
        await app.connect()
        print("✅ СОЕДИНЕНИЕ УСТАНОВЛЕНО! Прокси пропускает трафик Telegram.")
        
        # Проверяем, жива ли сессия (если файла test_session.session еще нет, тут скрипт просто завершится без ошибки)
        try:
            me = await app.get_me()
            print(f"✅ Успешный вход в аккаунт: {me.first_name}")
        except Exception:
            print("⚠️ Соединение есть, но аккаунт не авторизован (нужно ввести номер).")
            
    except TimeoutError:
        print("❌ ТАЙМАУТ: Прокси работает для сайтов, но не может достучаться до серверов Telegram (заблокировано провайдером прокси).")
    except ConnectionError:
        print("❌ ОШИБКА СОЕДИНЕНИЯ: Telegram принудительно разорвал связь (скорее всего IP прокси в черном списке ТГ).")
    except Exception as e:
        print(f"❌ НЕИЗВЕСТНАЯ ОШИБКА: {e}")
    finally:
        if app.is_connected:
            await app.disconnect()

if __name__ == "__main__":
    app.run(main())