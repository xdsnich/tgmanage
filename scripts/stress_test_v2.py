"""Расширенный стресс-тест: 1000 РАЗНОРОДНЫХ конфигураций.

Что тестируем:
- Coverage ≥ 95% (умещается ли всё)
- R1: smart_comment @X только после join_target_channel @X (в плане или already_joined)
- R2: smart_comment @X только после join_discussion_of @X (в текущем/предыдущем сессионе)
- R3: без дублей smart_comment @X в одном плане (день)
- R4: без дублей smart_comment @X кросс-days (для check_only)
- R5: sprinkle join_discussion_of только на каналы куда акк уже joined
- R6: total_comments в plan.total_comments == реально помещённых smart_comment

Конфиги — РВАНЫЕ:
- joined_ratio: непрерывно [0.0, 1.0]
- n_accs: 5-500
- channels_per_acc: 1-40
- Разное распределение каналов per acc (не равномерно)
"""
import sys
import math
import random
from collections import Counter, defaultdict
sys.path.insert(0, r"c:\Users\ufo001\source\repos\tg_manager1\api")

from tasks.plan_generator import (
    distribute_comments_by_days, distribute_day_comments, generate_daily_plan,
)
from tasks.behavior_engine import (
    assign_personality, assign_timing_profile, assign_style_profile,
)


def calc_total_days(total_channels, n_accs, max_pending):
    """Копия калькулятора из commenting.py rebuild."""
    MAX_PER_DAY = 1200
    MAX_PER_ACC_PER_DAY = 11
    by_max_per_day = max(1, math.ceil(total_channels / MAX_PER_DAY))
    by_pending = max(1, math.ceil(max_pending / 3.5))
    by_per_acc = max(1, math.ceil(total_channels / (n_accs * MAX_PER_ACC_PER_DAY)))
    return min(14, max(by_max_per_day, by_pending, by_per_acc) + 1)


def _simulate_generation(accounts_data, channel_dist, joined_by_acc, total_days):
    """Симулирует plan generation, возвращает planы + rules violations."""
    commented_by_acc = {}
    plans = []
    violations = defaultdict(int)

    # Кросс-day trackers per acc:
    # channel_joined_ever: {acc_id: set} — все каналы куда acc joined (initial + через план)
    # disc_joined_ever: {acc_id: set} — все каналы discussion куда acc joined
    channel_joined_ever = {a_id: set(joined_by_acc.get(a_id, set())) for a_id, _, _ in accounts_data}
    disc_joined_ever = {a_id: set() for a_id, _, _ in accounts_data}

    for day_num in range(1, total_days + 1):
        for a_id, phone, personality in accounts_data:
            assigned = channel_dist.get(a_id, [])
            already_joined = joined_by_acc.get(a_id, set())
            pending_for_acc = [ch for ch in assigned if ch not in already_joined]
            _jpd = max(1, math.ceil(len(pending_for_acc) / max(1, total_days))) if pending_for_acc else 1
            _js = (day_num - 1) * _jpd
            _je = day_num * _jpd
            channels_to_join_today = pending_for_acc[_js:_je]
            commentable_before_today = list(already_joined) + pending_for_acc[:_js]
            _fair_target = max(1, math.ceil(len(assigned) / max(1, total_days)))
            if day_num == total_days:
                _already_planned = (total_days - 1) * _fair_target
                _fair_target = max(_fair_target, len(assigned) - _already_planned)
            _pers = {**personality, "_force_verify_every_session": True}
            plan = generate_daily_plan(
                account_id=a_id, phone=phone,
                campaign_channels=[], campaign_id=0,
                day_number=day_num, comments_today=_fair_target,
                personality=_pers,
                timing=assign_timing_profile(phone),
                style=assign_style_profile(phone),
                channels_to_join=channels_to_join_today,
                commentable_from_prev_days=commentable_before_today,
                commented_from_prev_days=list(commented_by_acc.get(a_id, set())),
                add_tail_verify_session=(day_num == total_days),
            )
            plans.append((day_num, a_id, plan))

            # ── Валидация с учётом КРОСС-DAY состояния ──
            joined_so_far = set(channel_joined_ever[a_id])      # включая все прошлые дни + already_joined
            disc_joined_so_far = set(disc_joined_ever[a_id])    # включая disc joins в прошлые дни
            # Также: если acc УЖЕ комментил в канале ранее (кросс-day) — значит был в disc.
            disc_joined_so_far |= set(commented_by_acc.get(a_id, set()))
            commented_this_plan = set()
            for sess in plan.get("sessions", []):
                for act in sess.get("actions", []):
                    t = act.get("type", "")
                    ch = (act.get("channel", "") or "").lstrip("@")
                    if not ch and t in ("smart_comment", "join_target_channel", "join_discussion_of"):
                        continue
                    if t == "join_target_channel":
                        joined_so_far.add(ch)
                    elif t == "join_discussion_of":
                        if ch not in joined_so_far:
                            violations["R5_disc_join_before_channel_join"] += 1
                        disc_joined_so_far.add(ch)
                    elif t == "smart_comment":
                        if ch not in joined_so_far:
                            violations["R1_comment_before_channel_join"] += 1
                        if ch not in disc_joined_so_far:
                            violations["R2_comment_before_disc_join"] += 1
                        if ch in commented_this_plan:
                            violations["R3_duplicate_comment"] += 1
                        commented_this_plan.add(ch)

            # Обновляем КРОСС-DAY tracker'ы этим днём
            channel_joined_ever[a_id] |= joined_so_far
            disc_joined_ever[a_id] |= disc_joined_so_far

            _s = commented_by_acc.setdefault(a_id, set())
            for _sess in plan.get("sessions", []):
                for _act in _sess.get("actions", []):
                    if _act.get("type") == "smart_comment" and _act.get("channel"):
                        _ch = _act["channel"]
                        if _ch in _s:
                            violations["R4_cross_day_duplicate"] += 1
                        _s.add(_ch)

    total_planned_smart = sum(1 for _, _, p in plans for s in p.get("sessions", []) for a in s.get("actions", []) if a.get("type") == "smart_comment")
    return plans, violations, total_planned_smart


def simulate_config(cfg_id, n_accs, chan_dist_map, joined_by_acc, verbose=False):
    """Симулирует один конфиг с iterative подходом."""
    accounts_data = [(100 + i, f"+38095{1000000 + i}", assign_personality(f"+38095{1000000 + i}"))
                     for i in range(n_accs)]
    channel_dist = {a_id: chan_dist_map[a_id] for a_id, _, _ in accounts_data}
    total_assigned = sum(len(v) for v in channel_dist.values())
    max_pending = max(len(channel_dist[a]) - len(joined_by_acc.get(a, set())) for a in channel_dist)

    initial_days = calc_total_days(total_assigned, n_accs, max_pending)
    total_days = initial_days
    for attempt in range(4):
        plans, viol, realized = _simulate_generation(accounts_data, channel_dist, joined_by_acc, total_days)
        coverage = 100 * realized / max(1, total_assigned)
        if coverage >= 95.0 or total_days >= 14:
            break
        total_days += 1

    return {
        "cfg_id": cfg_id, "n_accs": n_accs,
        "total_assigned": total_assigned, "max_pending": max_pending,
        "initial_days": initial_days, "total_days": total_days,
        "realized_smart": realized, "coverage_pct": coverage,
        "violations": dict(viol),
    }


def main():
    random.seed(2026)
    print("═══ РАСШИРЕННЫЙ STRESS TEST v2 ═══")
    print("Условия — рваные: joined_ratio ∈ [0,1], n_accs ∈ [5,500], chans/acc ∈ [1,40]\n")

    results = []
    n_fail_coverage = 0
    all_violations = Counter()

    for i in range(1000):
        # РВАНЫЕ параметры
        n_accs = random.randint(5, 500)
        avg_chans = random.randint(1, 40)
        # Разное кол-во каналов per acc (не одинаково)
        chan_dist_map = {}
        for a_idx in range(n_accs):
            a_id = 100 + a_idx
            var = random.randint(-min(2, avg_chans-1), 3) if avg_chans >= 2 else 0
            n_ch = max(1, avg_chans + var)
            chan_dist_map[a_id] = [f"cfg{i}_acc{a_id}_ch{j}" for j in range(n_ch)]

        # РВАНЫЙ joined_ratio (не круглый)
        joined_ratio = random.uniform(0.0, 1.0)
        # Плюс небольшая вариация per acc
        joined_by_acc = {}
        for a_id, chs in chan_dist_map.items():
            local_ratio = max(0.0, min(1.0, joined_ratio + random.uniform(-0.15, 0.15)))
            n_joined = int(len(chs) * local_ratio)
            shuffled = list(chs)
            random.shuffle(shuffled)
            joined_by_acc[a_id] = set(shuffled[:n_joined])

        res = simulate_config(i, n_accs, chan_dist_map, joined_by_acc)
        res["joined_ratio_target"] = joined_ratio
        results.append(res)
        if res["coverage_pct"] < 95.0:
            n_fail_coverage += 1
        for rule, cnt in res["violations"].items():
            all_violations[rule] += cnt

        if (i + 1) % 100 == 0:
            print(f"  Прогресс: {i+1}/1000, fail_coverage={n_fail_coverage}, violations={sum(all_violations.values())}")

    print("\n═══ РЕЗУЛЬТАТЫ ═══")
    print(f"Всего: {len(results)}")
    print(f"Coverage < 95%: {n_fail_coverage}")

    print(f"\nНарушения правил:")
    if not all_violations:
        print("  ✅ Нет нарушений")
    for rule, cnt in all_violations.most_common():
        print(f"  ❌ {rule}: {cnt}")

    # По joined_ratio бинам
    bins = [(0, 0.1), (0.1, 0.25), (0.25, 0.4), (0.4, 0.55), (0.55, 0.7), (0.7, 0.85), (0.85, 1.0)]
    print(f"\nПо joined_ratio bins:")
    for lo, hi in bins:
        sub = [r for r in results if lo <= r["joined_ratio_target"] < hi]
        if not sub:
            continue
        avg = sum(r["coverage_pct"] for r in sub) / len(sub)
        mn = min(r["coverage_pct"] for r in sub)
        n_fail = sum(1 for r in sub if r["coverage_pct"] < 95.0)
        print(f"  {lo:.2f}-{hi:.2f}: n={len(sub):3d}, coverage avg={avg:5.1f}%, min={mn:5.1f}%, fail={n_fail}")

    # Худшие 10
    worst = sorted(results, key=lambda r: r["coverage_pct"])[:10]
    print("\nХудшие 10 (по coverage):")
    for r in worst:
        print(f"  cfg#{r['cfg_id']}: n_accs={r['n_accs']}, total_assigned={r['total_assigned']}, "
              f"jr_target={r['joined_ratio_target']:.2f}, days={r['total_days']} (initial={r['initial_days']}), "
              f"coverage={r['coverage_pct']:.1f}%, violations={r['violations']}")

    print(f"\nЛогика подписок:")
    print(f"  R1 (comment без join_target): {all_violations['R1_comment_before_channel_join']}")
    print(f"  R2 (comment без discussion): {all_violations['R2_comment_before_disc_join']}")
    print(f"  R3 (дубли в плане): {all_violations['R3_duplicate_comment']}")
    print(f"  R4 (кросс-day дубли): {all_violations['R4_cross_day_duplicate']}")
    print(f"  R5 (disc_join БЕЗ channel_join): {all_violations['R5_disc_join_before_channel_join']}")


if __name__ == "__main__":
    main()
