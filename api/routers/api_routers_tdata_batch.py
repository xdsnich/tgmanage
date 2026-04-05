"""
GramGPT API — Пакетный импорт TData (добавить в api/routers/tdata.py)
Эндпоинт: POST /import/tdata-batch

Принимает несколько ZIP-файлов с TData + опциональный proxy_id.
Каждый ZIP обрабатывается последовательно: распаковка → конвертация → сохранение в БД.
"""
from sqlalchemy.orm import joinedload
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Добавить этот эндпоинт в файл: api/routers/tdata.py
# (после существующего @router.post("/tdata"))
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@router.post("/tdata-batch")
async def import_tdata_batch(
    files: list[UploadFile] = File(...),
    proxy_id: int = Form(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Пакетный импорт нескольких TData архивов (ZIP).
    Каждый ZIP = один аккаунт Telegram Desktop.

    Параметры:
      - files: список ZIP-файлов с TData
      - proxy_id: (опц.) ID прокси для назначения всем импортированным аккаунтам

    Возвращает:
      - imported: количество успешных
      - errors: список ошибок
      - results: детали по каждому файлу (phone, status, account_id)
    """
    from models.proxy import Proxy

    await acc_svc.check_limit(db, current_user)

    # Проверяем прокси если указан
    proxy = None
    if proxy_id:
        proxy_r = await db.execute(
            select(Proxy).where(Proxy.id == proxy_id, Proxy.user_id == current_user.id)
        )
        proxy = proxy_r.scalar_one_or_none()
        if not proxy:
            raise HTTPException(status_code=404, detail=f"Прокси #{proxy_id} не найден")

    results = []
    imported = 0
    errors = []

    for file in files:
        file_name = file.filename or "unknown.zip"

        # Проверяем формат
        if not file_name.endswith(".zip"):
            errors.append({"file": file_name, "error": "Не ZIP файл"})
            results.append({"file": file_name, "status": "error", "error": "Не ZIP файл"})
            continue

        tmp_dir = tempfile.mkdtemp(prefix="gramgpt_tdata_batch_")

        try:
            # 1. Сохраняем ZIP
            zip_path = os.path.join(tmp_dir, "tdata.zip")
            content = await file.read()
            with open(zip_path, "wb") as f:
                f.write(content)

            # 2. Распаковываем
            try:
                with zipfile.ZipFile(zip_path, "r") as z:
                    z.extractall(tmp_dir)
            except zipfile.BadZipFile:
                errors.append({"file": file_name, "error": "Некорректный ZIP"})
                results.append({"file": file_name, "status": "error", "error": "Некорректный ZIP"})
                continue

            # 3. Ищем папку tdata
            tdata_path = None
            for root, dirs, files_list in os.walk(tmp_dir):
                if "tdata" in dirs:
                    tdata_path = os.path.join(root, "tdata")
                    break
                if any(f.startswith("key_") for f in files_list):
                    tdata_path = root
                    break

            if not tdata_path:
                if any(f.startswith("key_") for f in os.listdir(tmp_dir)):
                    tdata_path = tmp_dir
                else:
                    errors.append({"file": file_name, "error": "Папка TData не найдена в архиве"})
                    results.append({"file": file_name, "status": "error", "error": "TData не найдена"})
                    continue

            # 4. Конвертируем TData → session
            root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
            if root_dir not in sys.path:
                sys.path.insert(0, root_dir)

            api_config_cache = sys.modules.pop('config', None)
            for mod in ['config', 'ui', 'trust', 'tdata_importer', 'db', 'tg_client']:
                sys.modules.pop(mod, None)

            try:
                import tdata_importer as tdata_mod
                account_dict = await tdata_mod.import_tdata(tdata_path, "")
            finally:
                if api_config_cache:
                    sys.modules['config'] = api_config_cache

            if not account_dict:
                errors.append({"file": file_name, "error": "Конвертация TData не удалась"})
                results.append({"file": file_name, "status": "error", "error": "Конвертация TData не удалась"})
                continue

            # 5. Проверяем дубликаты
            phone = account_dict.get("phone", "")
            from sqlalchemy import select as sa_select
            existing = await db.execute(
                sa_select(TelegramAccount).options(joinedload(TelegramAccount.api_app)).where(
                    TelegramAccount.phone == phone,
                    TelegramAccount.user_id == current_user.id,
                )
            )
            if existing.scalar_one_or_none():
                errors.append({"file": file_name, "error": f"Аккаунт {phone} уже существует"})
                results.append({
                    "file": file_name, "phone": phone,
                    "status": "duplicate", "error": f"Уже существует",
                })
                continue

            # 6. Сохраняем аккаунт
            account = TelegramAccount(
                user_id=current_user.id,
                phone=phone,
                first_name=account_dict.get("first_name", ""),
                last_name=account_dict.get("last_name", ""),
                username=account_dict.get("username", ""),
                session_file=account_dict.get("session_file", ""),
                status=account_dict.get("status", "unknown"),
                trust_score=account_dict.get("trust_score", 0),
                proxy_id=proxy_id if proxy else None,
            )
            db.add(account)
            await db.flush()

            # Авто-назначение API ключа
            from services.api_apps import pick_best_app
            best_app = await pick_best_app(db, current_user.id)
            if best_app:
                account.api_app_id = best_app.id
                await db.flush()

            imported += 1
            results.append({
                "file": file_name,
                "phone": phone,
                "first_name": account_dict.get("first_name", ""),
                "username": account_dict.get("username", ""),
                "status": "ok",
                "account_id": account.id,
                "proxy_id": proxy_id if proxy else None,
            })

        except Exception as e:
            errors.append({"file": file_name, "error": str(e)[:200]})
            results.append({"file": file_name, "status": "error", "error": str(e)[:200]})

        finally:
            try:
                shutil.rmtree(tmp_dir)
            except:
                pass

    await db.commit()

    return {
        "imported": imported,
        "total": len(files),
        "errors": errors,
        "results": results,
        "proxy_id": proxy_id,
        "message": f"Импортировано {imported} из {len(files)} TData архивов. Ошибок: {len(errors)}",
    }
