"""
GramGPT — _fix_legacy_config.py
ОДНОРАЗОВЫЙ скрипт: убирает легаси tg_manager1/config.py с пути Python.

Что делает:
  1. Переименовывает tg_manager1/config.py → config.py.legacy
     (файл остаётся на диске для истории, но Python больше его не подхватит)
  2. Удаляет связанные .pyc в tg_manager1/__pycache__/config*
  3. Проверяет что api/config.py на месте и имеет DATABASE_URL

После этого ImportError 'cannot import name DATABASE_URL from config'
исчезнет навсегда — единственный config.py останется api/config.py.

Запуск:
  cd api
  python _fix_legacy_config.py

Безопасно: не трогает api/, не удаляет файлы (только переименовывает),
обратимо (можешь переименовать обратно если зачем-то нужно).
"""

import os
import sys
import glob


class C:
    R = "\033[31m"; G = "\033[32m"; Y = "\033[33m"
    BOLD = "\033[1m"; DIM = "\033[2m"; OFF = "\033[0m"


def main():
    api_dir    = os.path.dirname(os.path.abspath(__file__))
    parent_dir = os.path.dirname(api_dir)

    print(f"\n{C.BOLD}═══ FIX LEGACY CONFIG ═══{C.OFF}\n")
    print(f"  api/ dir:    {api_dir}")
    print(f"  parent dir:  {parent_dir}\n")

    # ── 1. Проверяем что api/config.py существует и валиден ──
    api_config = os.path.join(api_dir, "config.py")
    if not os.path.exists(api_config):
        print(f"  {C.R}✕ {api_config} не существует!{C.OFF}")
        print(f"  {C.R}  Это означает что api/config.py отсутствует — что-то совсем не так.{C.OFF}")
        return 1

    try:
        with open(api_config, "r", encoding="utf-8") as f:
            content = f.read()
        if "DATABASE_URL" not in content:
            print(f"  {C.R}✕ {api_config} не содержит DATABASE_URL{C.OFF}")
            return 1
        print(f"  {C.G}✓ api/config.py OK (содержит DATABASE_URL){C.OFF}")
    except Exception as e:
        print(f"  {C.R}✕ Не могу прочитать {api_config}: {e}{C.OFF}")
        return 1

    # ── 2. Ищем легаси config.py в parent ──
    legacy_config = os.path.join(parent_dir, "config.py")
    if not os.path.exists(legacy_config):
        print(f"  {C.G}✓ Легаси {legacy_config} уже отсутствует (видимо ты его уже переименовал){C.OFF}")
    else:
        # Делаем уникальное имя для бэкапа чтобы не перезаписать существующий
        backup_path = legacy_config + ".legacy"
        suffix = 1
        while os.path.exists(backup_path):
            backup_path = legacy_config + f".legacy.{suffix}"
            suffix += 1

        try:
            os.rename(legacy_config, backup_path)
            print(f"  {C.G}✓ Переименован:{C.OFF}")
            print(f"    {legacy_config}")
            print(f"    → {backup_path}")
        except Exception as e:
            print(f"  {C.R}✕ Не получилось переименовать: {e}{C.OFF}")
            print(f"  {C.Y}  Попробуй вручную: ren \"{legacy_config}\" config.py.legacy{C.OFF}")
            return 1

    # ── 3. Удаляем .pyc файлы кеша легаси-config ──
    pycache_dir = os.path.join(parent_dir, "__pycache__")
    if os.path.exists(pycache_dir):
        removed = 0
        for pyc in glob.glob(os.path.join(pycache_dir, "config*")):
            try:
                os.remove(pyc)
                removed += 1
            except Exception as e:
                print(f"  {C.Y}⚠ Не удалось удалить {pyc}: {e}{C.OFF}")
        if removed:
            print(f"  {C.G}✓ Удалено {removed} закешированных .pyc файлов легаси-config{C.OFF}")
        else:
            print(f"  {C.DIM}  (кеша легаси-config в __pycache__ не было){C.OFF}")
    else:
        print(f"  {C.DIM}  ({pycache_dir} не существует){C.OFF}")

    # ── 4. Финальная проверка — пробуем импортнуть api/config.py ──
    print()
    print(f"{C.BOLD}Финальная проверка:{C.OFF}")

    # Очищаем sys.path
    sys.path[:] = [p for p in sys.path
                   if os.path.normcase(os.path.abspath(p) if p else os.getcwd()) != os.path.normcase(parent_dir)]
    if api_dir not in sys.path:
        sys.path.insert(0, api_dir)
    for mod in list(sys.modules):
        if mod == "config" or mod.startswith("config."):
            del sys.modules[mod]

    try:
        import config as cfg
        actual = os.path.normcase(os.path.abspath(cfg.__file__))
        expected = os.path.normcase(api_config)
        if actual == expected:
            print(f"  {C.G}✓ config грузится из api/config.py{C.OFF}")
        else:
            print(f"  {C.R}✕ config всё ещё грузится не оттуда:{C.OFF}")
            print(f"    actual:   {cfg.__file__}")
            print(f"    expected: {api_config}")
            return 1

        if hasattr(cfg, "DATABASE_URL"):
            print(f"  {C.G}✓ DATABASE_URL доступна{C.OFF}")
        else:
            print(f"  {C.R}✕ DATABASE_URL отсутствует в импортированном config{C.OFF}")
            return 1
    except Exception as e:
        print(f"  {C.R}✕ Импорт config упал: {type(e).__name__}: {e}{C.OFF}")
        return 1

    print()
    print(f"{C.G}{C.BOLD}═══ ГОТОВО ═══{C.OFF}")
    print(f"{C.G}Теперь python test_smoke.py должен показать 12/12 OK.{C.OFF}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
