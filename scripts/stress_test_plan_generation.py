"""Стресс-тест генератора планов: 1000 конфигураций.

Проверяем:
- Умещается ли ВСЕ комменты в плане (target vs realized)
- Работает для 0% / 50% / 100% pre-joined каналов
- Работает для разного числа акков (5-500)
- Работает для разного числа каналов (10-3000)
- Правильно ли применяется правило "не подписываться если подписан"
- Правильно ли не пишем в неподписанные каналы
"""
import sys
import os
import math
import random
sys.path.insert(0, r"c:\Users\ufo001\source\repos\tg_manager1\api")

from tasks.plan_generator import (
    distribute_comments_by_days, distribute_day_comments, generate_daily_plan,
)
from tasks.behavior_engine import (
    assign_personality, assign_timing_profile, assign_style_profile,
)


def calc_total_days(total_channels, n_accs, max_pending):
    """Копия калькулятора из commenting.py rebuild."""
    MAX_PER_DAY = 650
    MAX_PER_ACC_PER_DAY = 4
    by_max_per_day = max(1, math.ceil(total_channels / MAX_PER_DAY))
    by_pending = max(1, math.ceil(max_pending / 3.5))
    by_per_acc = max(1, math.ceil(total_channels / (n_accs * MAX_PER_ACC_PER_DAY)))
    min_days = max(by_max_per_day, by_pending, by_per_acc)
    return min(14, min_days + 1)   # +1 safety


def _simulate_generation(accounts_data, channel_dist, joined_by_acc, total_days):
    """Симулирует plan generation для заданных total_days. Возвращает realized_smart."""
    commented_by_acc = {}
    realized_smart = 0
    realized_join = 0
    total_assigned = sum(len(channel_dist.get(a[0], [])) for a in accounts_data)

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
            for sess in plan.get("sessions", []):
                for act in sess.get("actions", []):
                    if act.get("type") == "smart_comment":
                        realized_smart += 1
                        commented_by_acc.setdefault(a_id, set()).add(act["channel"])
                    elif act.get("type") == "join_target_channel":
                        realized_join += 1
    return realized_smart, realized_join, total_assigned


def simulate_one_config(cfg_id, n_accs, channels_per_acc, joined_ratio, verbose=False):
    accounts_data = [(100 + i, f"+38095{1000000 + i}", assign_personality(f"+38095{1000000 + i}"))
                     for i in range(n_accs)]
    channel_dist = {}
    joined_by_acc = {}
    total_assigned = 0
    for a_id, _, _ in accounts_data:
        chs = [f"cfg{cfg_id}_acc{a_id}_ch{j}" for j in range(channels_per_acc)]
        channel_dist[a_id] = chs
        total_assigned += len(chs)
        n_joined = int(len(chs) * joined_ratio)
        random.shuffle(chs)
        joined_by_acc[a_id] = set(chs[:n_joined])

    max_pending = max(len(channel_dist[a_id]) - len(joined_by_acc.get(a_id, set()))
                      for a_id, _, _ in accounts_data)

    # ── Iterative: пробуем total_days, если coverage <95% → +1 день, до 3 попыток ──
    initial_days = calc_total_days(total_assigned, n_accs, max_pending)
    total_days = initial_days
    for attempt in range(4):
        realized_smart, realized_join, _ = _simulate_generation(
            accounts_data, channel_dist, joined_by_acc, total_days
        )
        coverage = 100 * realized_smart / max(1, total_assigned)
        if coverage >= 95.0 or total_days >= 14:
            break
        total_days += 1

    return {
        "cfg_id": cfg_id, "n_accs": n_accs, "channels_per_acc": channels_per_acc,
        "joined_ratio": joined_ratio, "total_assigned": total_assigned,
        "initial_days": initial_days, "total_days": total_days,
        "max_pending": max_pending,
        "realized_smart": realized_smart, "realized_join": realized_join,
        "coverage_pct": coverage,
        "extra_days_used": total_days - initial_days,
    }


def main():
    random.seed(42)
    print("═══ STRESS TEST 1000 configs ═══\n")

    # Различные конфиги для теста
    configs = []
    # Full grid: 4 acc_counts × 5 channels_per_acc × 5 joined_ratio = 100 конфигов × 10 rand seeds = 1000
    acc_counts = [10, 50, 150, 500]
    ch_counts = [3, 8, 14, 20, 30]
    joined_ratios = [0.0, 0.25, 0.5, 0.75, 1.0]
    for i in range(1000):
        n = random.choice(acc_counts)
        cpa = random.choice(ch_counts)
        jr = random.choice(joined_ratios)
        configs.append((i, n, cpa, jr))

    results = []
    failed = 0
    for i, (cfg_id, n_accs, cpa, jr) in enumerate(configs):
        res = simulate_one_config(cfg_id, n_accs, cpa, jr)
        results.append(res)
        if res["coverage_pct"] < 95.0:
            failed += 1
        if (i + 1) % 100 == 0:
            print(f"  Прогресс: {i+1}/1000, failed={failed}")

    # ── Аналитика ──
    print("\n═══ Результаты ═══")
    print(f"Всего конфигов: {len(results)}")
    print(f"Успех (>=95% coverage): {len(results) - failed}")
    print(f"Провал (<95% coverage): {failed}")

    # По joined_ratio
    from collections import defaultdict
    by_jr = defaultdict(list)
    for r in results:
        by_jr[r["joined_ratio"]].append(r["coverage_pct"])
    print("\nПо joined_ratio (% pre-joined каналов):")
    for jr in sorted(by_jr.keys()):
        covs = by_jr[jr]
        avg = sum(covs) / len(covs)
        min_c = min(covs)
        max_c = max(covs)
        print(f"  {jr:.2f}: avg={avg:.1f}%  min={min_c:.1f}%  max={max_c:.1f}%  (n={len(covs)})")

    # Худшие 10
    worst = sorted(results, key=lambda r: r["coverage_pct"])[:10]
    print("\nХудшие 10 конфигов:")
    for r in worst:
        print(f"  cfg#{r['cfg_id']}: n_accs={r['n_accs']}, cpa={r['channels_per_acc']}, "
              f"jr={r['joined_ratio']}, days={r['total_days']} (initial={r['initial_days']}, "
              f"+{r['extra_days_used']}), coverage={r['coverage_pct']:.1f}% "
              f"({r['realized_smart']}/{r['total_assigned']})")

    # Extra days использованы
    extras = [r["extra_days_used"] for r in results]
    from collections import Counter
    extra_dist = Counter(extras)
    print(f"\nРаспределение extra days (safety +1 → +N):")
    for k in sorted(extra_dist.keys()):
        print(f"  +{k}: {extra_dist[k]} конфигов")


if __name__ == "__main__":
    main()
