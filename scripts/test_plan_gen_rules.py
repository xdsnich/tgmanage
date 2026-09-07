"""Тест генерации планов: 1000 прогонов, проверяем ВСЕ правила.

Правила:
  R1. Каждый smart_comment @X ДОЛЖЕН иметь в этом плане (в этой сессии
      или в предыдущей) join_discussion_of @X.
  R2. Каждый smart_comment @X ДОЛЖЕН иметь либо join_target_channel @X
      РАНЕЕ в этом же плане, либо X должен быть в commentable_before_today
      (= уже был подписан ДО начала этого дня).
  R3. Никаких DUPLICATE smart_comment @X внутри одного плана-дня
      (для check_only особенно важно — 1 коммент на канал).
  R4. join_discussion_of @X targets только те каналы, в которые акк
      уже подписался (channels_to_join_today ИЛИ commentable_before_today
      ИЛИ уже joined_today в этой сессии).
  R5. Нет двух подряд join_discussion_of на один и тот же канал в одном плане.
  R6. join_target_channel @X ВСЕГДА до smart_comment @X в этом плане.
  R7. Нет ЛИШНИХ smart_comment @X (без соответствующей подготовки).
"""
import os
import sys
import json
from collections import defaultdict

sys.path.insert(0, r"c:\Users\ufo001\source\repos\tg_manager1\api")
from dotenv import load_dotenv
load_dotenv(r"c:\Users\ufo001\source\repos\tg_manager1\api\.env")

from tasks.plan_generator import generate_daily_plan


def validate_plan(plan: dict, assigned: list, channels_to_join: list,
                  commentable_before_today: list) -> list:
    """Возвращает список сообщений о нарушениях. Пустой = всё ок."""
    errs = []
    sessions = plan.get("sessions", [])

    # Проходим ПО ПОРЯДКУ действий во всех сессиях
    joined_at_step = set(commentable_before_today or [])   # каналы куда акк подписался
    disc_joined_at_step = set()                             # каналы где акк в discussion
    commented_in_plan = set()                                # каналы где акк уже комментил в этом плане

    for si, s in enumerate(sessions):
        actions = s.get("actions", [])
        for ai, a in enumerate(actions):
            t = a.get("type", "")
            ch = (a.get("channel", "") or "").lstrip("@")
            if not ch and t in ("smart_comment", "join_target_channel", "join_discussion_of"):
                errs.append(f"[s{si} a{ai}] {t} без channel")
                continue

            if t == "join_target_channel":
                joined_at_step.add(ch)

            elif t == "join_discussion_of":
                # R4: канал должен быть уже joined
                if ch not in joined_at_step:
                    errs.append(f"[s{si} a{ai}] join_discussion_of @{ch}: канал НЕ был join'нут ранее")
                # R5: не повторять сразу (в идеале не в одном плане)
                if ch in disc_joined_at_step:
                    errs.append(f"[s{si} a{ai}] join_discussion_of @{ch}: ПОВТОР (уже был в этом плане)")
                disc_joined_at_step.add(ch)

            elif t == "smart_comment":
                # R2: канал joined
                if ch not in joined_at_step:
                    errs.append(f"[s{si} a{ai}] smart_comment @{ch}: канал НЕ joined ранее")
                # R1: discussion joined
                if ch not in disc_joined_at_step:
                    errs.append(f"[s{si} a{ai}] smart_comment @{ch}: discussion НЕ joined ранее (R1 нарушен)")
                # R3: дубль
                if ch in commented_in_plan:
                    errs.append(f"[s{si} a{ai}] smart_comment @{ch}: ДУБЛЬ в этом плане")
                commented_in_plan.add(ch)

    return errs


def run_batch(n_plans: int = 1000, check_only: bool = True):
    """Генерирует n_plans планов и проверяет каждый."""
    from tasks.behavior_engine import (
        assign_personality, assign_timing_profile, assign_style_profile,
    )
    import random
    random.seed(42)

    total_errs = 0
    plans_with_errs = 0
    err_categories = defaultdict(int)
    first_bad_plan = None

    for i in range(n_plans):
        # Различные конфигурации
        n_channels_total = random.randint(3, 30)
        assigned = [f"ch_{i}_{j}" for j in range(n_channels_total)]

        n_already_joined = random.randint(0, n_channels_total)
        already_joined = assigned[:n_already_joined]
        pending = assigned[n_already_joined:]

        total_days = random.choice([1, 3, 5, 7])
        day_num = random.randint(1, total_days)

        # Как в rebuild:
        _jpd = max(1, len(pending) // total_days) if pending else 0
        _js = (day_num - 1) * _jpd
        _je = day_num * _jpd
        channels_to_join_today = pending[_js:_je]
        commentable_before_today = list(already_joined) + pending[:_js]

        # comments_today
        comments_today = min(len(commentable_before_today) + len(channels_to_join_today),
                             random.randint(1, 8))

        phone = f"+38095{random.randint(1000000, 9999999)}"
        personality = assign_personality(phone)
        if check_only:
            personality = {**personality, "_force_verify_every_session": True}

        try:
            plan = generate_daily_plan(
                account_id=100 + i,
                phone=phone,
                campaign_channels=assigned,
                campaign_id=999,
                day_number=day_num,
                comments_today=comments_today,
                personality=personality,
                timing=assign_timing_profile(phone),
                style=assign_style_profile(phone),
                channels_to_join=channels_to_join_today,
                commentable_from_prev_days=commentable_before_today,
                add_tail_verify_session=(day_num == total_days),
            )
        except Exception as e:
            total_errs += 1
            plans_with_errs += 1
            err_categories[f"crash: {type(e).__name__}"] += 1
            continue

        # R6: total_comments из плана должен совпадать с реальным числом smart_comment
        _n_smart = sum(1 for s in plan.get("sessions", []) for a in s.get("actions", []) if a.get("type") == "smart_comment")
        _tc = plan.get("total_comments", -1)
        if _tc != _n_smart:
            plans_with_errs += 1
            err_categories[f"R6: total_comments mismatch (tc={_tc}, real={_n_smart})"] += 1

        errs = validate_plan(plan, assigned, channels_to_join_today, commentable_before_today)
        if errs:
            plans_with_errs += 1
            total_errs += len(errs)
            for e in errs:
                # Категория = после "]", до "@"
                rest = e.split("]", 1)[-1].strip()
                # Оставляем только "type: причина"
                if ":" in rest:
                    t_part, cause = rest.split(":", 1)
                    # Уберём @channel
                    t_part = t_part.split("@")[0].strip()
                    cat = f"{t_part}: {cause.strip()}"
                else:
                    cat = rest
                err_categories[cat] += 1
            if first_bad_plan is None:
                first_bad_plan = {
                    "i": i,
                    "assigned": assigned,
                    "already_joined": already_joined,
                    "channels_to_join_today": channels_to_join_today,
                    "commentable_before_today": commentable_before_today,
                    "comments_today": comments_today,
                    "day_num": day_num,
                    "total_days": total_days,
                    "plan": plan,
                    "errs": errs,
                }

    print(f"\n=== TEST: {n_plans} planов, check_only={check_only} ===")
    print(f"  Планов с ошибками: {plans_with_errs} ({100*plans_with_errs/n_plans:.1f}%)")
    print(f"  Всего нарушений: {total_errs}")
    print(f"  ✅ Чистых планов: {n_plans - plans_with_errs}")
    if err_categories:
        print(f"\n  Категории нарушений:")
        for cat, cnt in sorted(err_categories.items(), key=lambda x: -x[1])[:15]:
            print(f"    x{cnt}  {cat}")

    if first_bad_plan:
        print(f"\n  --- ПЕРВЫЙ ПЛОХОЙ ПЛАН (i={first_bad_plan['i']}) ---")
        print(f"  assigned={len(first_bad_plan['assigned'])} channels")
        print(f"  already_joined={first_bad_plan['already_joined']}")
        print(f"  channels_to_join_today={first_bad_plan['channels_to_join_today']}")
        print(f"  commentable_before_today={first_bad_plan['commentable_before_today']}")
        print(f"  comments_today={first_bad_plan['comments_today']} day={first_bad_plan['day_num']}/{first_bad_plan['total_days']}")
        for si, s in enumerate(first_bad_plan["plan"].get("sessions", [])):
            print(f"  Session {si}: connect={s.get('connect_at_hour')}:{s.get('connect_at_minute'):02d}, skipped={s.get('skipped', False)}")
            for ai, a in enumerate(s.get("actions", [])):
                ch = a.get("channel", "")
                extra = f" @{ch}" if ch else ""
                print(f"    [{ai}] {a.get('type','?'):25s}{extra}")
        print(f"  🚨 ОШИБКИ:")
        for e in first_bad_plan["errs"]:
            print(f"    {e}")

    return plans_with_errs, total_errs, err_categories


if __name__ == "__main__":
    print("=" * 70)
    print("PLAN GENERATOR STRESS TEST")
    print("=" * 70)

    # 1. check_only mode
    p1, t1, cat1 = run_batch(n_plans=1000, check_only=True)
    # 2. regular mode
    p2, t2, cat2 = run_batch(n_plans=1000, check_only=False)

    print("\n" + "=" * 70)
    print(f"ИТОГО: {p1 + p2} проблемных planов из 1000")
    print(f"       {t1 + t2} всего нарушений правил")
    print("=" * 70)

    # Показать 3 примера "плохих" планов
    print("\n=== ПРИМЕРЫ ПЛАНОВ ===")
    import random
    random.seed(1)
    for _ in range(3):
        n = 12
        pending = [f"ch_p_{i}" for i in range(n)]
        already = ["ch_j_1", "ch_j_2", "ch_j_3", "ch_j_4"]
        assigned = already + pending
        total_days = 3
        day_num = 1
        _jpd = max(1, len(pending) // total_days)
        _js = (day_num - 1) * _jpd
        channels_to_join = pending[_js:_js+_jpd]
        commentable = list(already)
        comments_today = 4

        from tasks.behavior_engine import (
            assign_personality, assign_timing_profile, assign_style_profile,
        )
        phone = f"+38095{random.randint(1000000, 9999999)}"
        personality = {**assign_personality(phone), "_force_verify_every_session": True}

        plan = generate_daily_plan(
            account_id=999, phone=phone,
            campaign_channels=assigned, campaign_id=999,
            day_number=day_num, comments_today=comments_today,
            personality=personality,
            timing=assign_timing_profile(phone),
            style=assign_style_profile(phone),
            channels_to_join=channels_to_join,
            commentable_from_prev_days=commentable,
        )

        errs = validate_plan(plan, assigned, channels_to_join, commentable)
        print(f"\n--- План (assigned={len(assigned)}, joined={len(already)}, join_today={channels_to_join}, comments_today={comments_today}) ---")
        for si, s in enumerate(plan.get("sessions", [])):
            print(f"  Session {si}: connect={s.get('connect_at_hour')}:{s.get('connect_at_minute'):02d}")
            for ai, a in enumerate(s.get("actions", [])):
                ch = a.get("channel", "")
                extra = f" @{ch}" if ch else ""
                print(f"    [{ai}] {a.get('type','?'):25s}{extra}")
        if errs:
            print(f"  🚨 ОШИБКИ:")
            for e in errs:
                print(f"    {e}")
        else:
            print(f"  ✅ План чистый")
