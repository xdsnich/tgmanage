"""
cleanup_ghost_channels.py — одноразовая очистка ложных каналов
из ParsedChannel и TgstatCheckedChannel.

Причина: extractor до фикса засчитывал fallback-страницу TGStat
«Channel not found - 404» как живой канал. В БД накопились записи
с title вроде "Channel not found - 404" — их надо убрать.

Что считаем ghost'ом:
  title ILIKE ANY:
    '%channel not found%'
    '%page does not exist%'
    '%канал не найден%'
    '%страница не существует%'
    '%not found%'
    '% - 404%'
    '404 -%'

Дополнительно опционально можно посетить каждого «подозрительного»
и проверить через httpx на t.me/@X редирект на "not found", но это
медленно; пока — просто по title.

Запуск:
    cd api
    python -m scripts.cleanup_ghost_channels          # dry-run
    python -m scripts.cleanup_ghost_channels --apply  # реально удалить
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Позволяем скрипту импортировать модули api/
_HERE = Path(__file__).resolve().parent
_API = _HERE.parent / "api"
if str(_API) not in sys.path:
    sys.path.insert(0, str(_API))


GHOST_TITLE_PATTERNS = [
    "%channel not found%",
    "%page does not exist%",
    "%канал не найден%",
    "%страница не существует%",
    "%канал был удалён%",
    "%channel was deleted%",
    "%not found - 404%",
    "%does not exist - 404%",
    "404 -%",
    "% - 404%",
]


async def main(apply: bool) -> int:
    from sqlalchemy import select, or_, delete as sa_delete
    from utils.db_pool import async_session as Session
    from models.parsed_channel import ParsedChannel
    from models.tgstat_checked_channel import TgstatCheckedChannel

    total_pc_hit = 0
    total_tcc_hit = 0

    async with Session() as db:
        # ── ParsedChannel ─────────────────────────────────────
        conds = [ParsedChannel.title.ilike(p) for p in GHOST_TITLE_PATTERNS]
        rows = (await db.execute(
            select(ParsedChannel.id, ParsedChannel.username, ParsedChannel.title, ParsedChannel.user_id)
            .where(or_(*conds))
        )).all()
        print(f"═══ ParsedChannel — найдено {len(rows)} ghost-записей ═══")
        for r in rows[:60]:
            print(f"  id={r[0]:>6}  user={r[3]}  @{r[1]:<32}  title={r[2][:60] if r[2] else ''!r}")
        if len(rows) > 60:
            print(f"  … и ещё {len(rows) - 60}")
        total_pc_hit = len(rows)

        # ── TgstatCheckedChannel ──────────────────────────────
        # Ghost'ы там тоже — извлечём и удалим, чтобы следующий скрейп
        # перепроверил (и снова получил 404 → is_dead → просто пропуск).
        conds_tcc = [TgstatCheckedChannel.title.ilike(p) for p in GHOST_TITLE_PATTERNS]
        rows_tcc = (await db.execute(
            select(TgstatCheckedChannel.id, TgstatCheckedChannel.username,
                   TgstatCheckedChannel.title, TgstatCheckedChannel.user_id)
            .where(or_(*conds_tcc))
        )).all()
        print(f"\n═══ TgstatCheckedChannel — найдено {len(rows_tcc)} ghost-записей ═══")
        for r in rows_tcc[:60]:
            print(f"  id={r[0]:>6}  user={r[3]}  @{r[1]:<32}  title={r[2][:60] if r[2] else ''!r}")
        if len(rows_tcc) > 60:
            print(f"  … и ещё {len(rows_tcc) - 60}")
        total_tcc_hit = len(rows_tcc)

        if not apply:
            print("\nDry-run — ничего не удалено. Запусти с --apply чтобы удалить.")
            return 0

        # ── Реальное удаление ──
        if total_pc_hit:
            res = await db.execute(
                sa_delete(ParsedChannel).where(or_(*conds))
            )
            print(f"\n🗑 Удалено из parsed_channels: {res.rowcount}")

        if total_tcc_hit:
            res = await db.execute(
                sa_delete(TgstatCheckedChannel).where(or_(*conds_tcc))
            )
            print(f"🗑 Удалено из tgstat_checked_channels: {res.rowcount}")

        await db.commit()
        print("\n✓ Готово.")

    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="Реально удалить (без флага — только показать что найдено)")
    args = ap.parse_args()

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    sys.exit(asyncio.run(main(apply=args.apply)))
