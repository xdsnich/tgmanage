"""
utils/ip_scheduler.py — разнесение сессий аккаунтов, сидящих на одном IP.

Задача: чтобы на одном физическом IP никогда не было двух Telegram-сессий
ближе чем MIN_GAP минут, и чтобы это достигалось РАСПИСАНИЕМ, а не
откладыванием в рантайме.

Почему не хватало ip_throttle: он реактивен — обнаружив совпадение, он
не разносит сессии, а откладывает вторую. Если к моменту повтора прошло
больше 4 часов от планового времени, сессия выбрасывается совсем
(инцидент 2026-07-14: потеряно 126 smart_comment из 285 запланированных).
Замер 2026-08-22: 37.5% пар подключений на одном IP укладывались в 15 минут.

Два механизма:

1. ХРОНОТИП аккаунта — постоянный «режим дня», детерминированный по телефону
   (как personality в behavior_engine). Аккаунты на одном IP живут в разное
   время суток и расходятся сами, ещё до всякого резервирования. Заодно
   лечится неестественная картина: раньше правило «2 ночные сессии каждому»
   делало 00-05 самыми загруженными часами суток, а 06-07 — мёртвой зоной.

2. РАЗМЕЩЕНИЕ С ГАРАНТИЕЙ ЗАЗОРА — сессии всех аккаунтов IP раскладываются
   на одной общей временной оси. Интервалы получаются неровные (случайные),
   но ни один не меньше MIN_GAP.

Модуль чистый: без БД, без Celery, без Telethon. Принимает данные — отдаёт
расписание. Так же, как plan_generator.
"""

import hashlib
import random

# ── Хронотипы ───────────────────────────────────────────────
# Окна намеренно перекрываются: реальные люди не живут по расписанию,
# и полностью непересекающиеся окна выглядели бы искусственно.
# `weight` — доля аккаунтов, `windows` — часы (from, to), to не включается.
CHRONOTYPES = [
    {
        "name": "early_bird",       # встаёт рано, вечером спит
        # начало в 5, а не в 6: иначе граница с окном night_owl сходилась
        # ровно на 05:00 и в этом часу не было НИ ОДНОЙ сессии по всей
        # системе — ровный шов в суточной картине, заметный со стороны.
        "windows": [(5, 14)],
        "weight": 20,
    },
    {
        "name": "daytime",          # обычный дневной режим
        "windows": [(9, 19)],
        "weight": 30,
    },
    {
        "name": "evening",          # раскачивается после обеда
        "windows": [(15, 24)],
        "weight": 30,
    },
    {
        "name": "night_owl",        # вечер + ночь, окно переходит через полночь
        "windows": [(21, 24), (0, 6)],
        "weight": 20,
    },
]

MIN_GAP_MIN = 20          # минимум между сессиями РАЗНЫХ аккаунтов на одном IP
SELF_GAP_MIN = 45         # минимум между сессиями ОДНОГО аккаунта
_PLACE_ATTEMPTS = 300

# ── Вариативность ───────────────────────────────────────────
# Хронотип постоянный (человек не меняет режим жизни каждый день), но
# КОНКРЕТНЫЕ времена обязаны быть разными каждый день, иначе получим
# ровно тот же отпечаток, только сдвинутый.
#
# Два источника разброса:
#  1) DAY_SHIFT — окно целиком плавает день ото дня: сегодня встал
#     раньше, завтра позже.
#  2) OUTLIER_CHANCE — отдельная сессия иногда выпадает из окна вовсе.
#     Без этого «дневной» аккаунт НИКОГДА не появлялся бы вечером, а
#     «никогда» — это тоже устойчивый признак.
DAY_SHIFT_CHOICES = [-90, -60, -45, -30, -15, 0, 0, 15, 30, 45, 60, 90]
OUTLIER_CHANCE = 0.12
OUTLIER_WINDOW = [(7, 24)]   # в это время телефон может взять кто угодно


def assign_chronotype(phone: str) -> dict:
    """Постоянный хронотип по номеру. Один телефон — всегда один режим."""
    if not phone:
        return CHRONOTYPES[1]
    h = int(hashlib.md5((phone + "_chrono").encode()).hexdigest(), 16)
    total = sum(c["weight"] for c in CHRONOTYPES)
    pick = h % total
    acc = 0
    for c in CHRONOTYPES:
        acc += c["weight"]
        if pick < acc:
            return c
    return CHRONOTYPES[-1]


def _windows_to_minutes(windows) -> list[tuple[int, int]]:
    """[(6,14)] → [(360, 840)] в минутах от полуночи."""
    out = []
    for (a, b) in windows:
        start, end = a * 60, b * 60
        if end > start:
            out.append((start, min(end, 1440)))
    return out


def _shift_segments(segments, shift_min: int) -> list[tuple[int, int]]:
    """Сдвигает окно на shift_min минут с заворотом через полночь."""
    out = []
    for s, e in segments:
        s2, e2 = s + shift_min, e + shift_min
        # нормализуем в 0..1440, при выходе за край режем на два куска
        if s2 < 0 and e2 <= 0:
            out.append((s2 + 1440, e2 + 1440))
        elif s2 < 0 < e2:
            out.append((s2 + 1440, 1440))
            out.append((0, e2))
        elif s2 < 1440 < e2:
            out.append((s2, 1440))
            out.append((0, e2 - 1440))
        elif s2 >= 1440:
            out.append((s2 - 1440, e2 - 1440))
        else:
            out.append((s2, e2))
    return [(max(0, s), min(1440, e)) for s, e in out if e > s]


def _day_segments(chrono: dict, rnd) -> list[tuple[int, int]]:
    """Окно хронотипа на КОНКРЕТНЫЙ день — базовое плюс случайный сдвиг."""
    base = _windows_to_minutes(chrono["windows"])
    return _shift_segments(base, rnd.choice(DAY_SHIFT_CHOICES)) or base


def _random_point(segments, rnd) -> int:
    """Случайная минута внутри набора отрезков, пропорционально их длине."""
    total = sum(e - s for s, e in segments)
    if total <= 0:
        return rnd.randint(0, 1439)
    x = rnd.uniform(0, total)
    acc = 0.0
    for s, e in segments:
        span = e - s
        if x <= acc + span:
            return int(s + (x - acc))
        acc += span
    return int(segments[-1][1] - 1)


def _fits(t: int, placed_all: list[int], placed_self: list[int],
          min_gap: int, self_gap: int) -> bool:
    if any(abs(t - p) < min_gap for p in placed_all):
        return False
    if any(abs(t - p) < self_gap for p in placed_self):
        return False
    return True


def _best_free_slot(segments, placed_all, placed_self, min_gap, self_gap, rnd):
    """Fallback: ищем самый широкий свободный промежуток внутри окна."""
    candidates = []
    for s, e in segments:
        # границы занятых зон внутри отрезка
        marks = [s - min_gap] + sorted(p for p in placed_all if s - min_gap <= p <= e + min_gap) + [e + min_gap]
        for i in range(len(marks) - 1):
            lo = marks[i] + min_gap
            hi = marks[i + 1] - min_gap
            if hi > lo:
                candidates.append((hi - lo, lo, hi))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    # берём из трёх самых широких — чтобы не было детерминизма
    width, lo, hi = rnd.choice(candidates[:3])
    for _ in range(40):
        t = rnd.randint(int(lo), int(hi))
        if not any(abs(t - p) < self_gap for p in placed_self):
            return t
    return int((lo + hi) // 2)


def _clip_segments(segments, not_before: int) -> list[tuple[int, int]]:
    """Отрезает всё, что раньше not_before (минуты от полуночи)."""
    out = []
    for s, e in segments:
        s2 = max(s, not_before)
        if e > s2:
            out.append((s2, e))
    return out


def allocate_ip_day(accounts, min_gap_min: int = MIN_GAP_MIN,
                    self_gap_min: int = SELF_GAP_MIN, seed=None,
                    not_before_min: int = None) -> dict:
    """Раскладывает сессии всех аккаунтов одного IP на общей оси суток.

    accounts: [{"id": int, "phone": str, "sessions": int}, ...]
    Возвращает {account_id: [(hour, minute), ...]} — времена отсортированы.

    Гарантия: между ЛЮБЫМИ двумя сессиями разных аккаунтов ≥ min_gap_min,
    между сессиями одного аккаунта ≥ self_gap_min.

    `reserved` можно передать через accounts[i]["reserved"] — уже занятые
    минуты на этом IP (например, планами других кампаний на тот же день).
    """
    rnd = random.Random(seed)

    # Заявки: по одной на каждую сессию каждого аккаунта
    requests = []
    chrono_by_acc = {}
    outlier_segs = _windows_to_minutes(OUTLIER_WINDOW)
    for a in accounts:
        ch = assign_chronotype(a.get("phone", ""))
        chrono_by_acc[a["id"]] = ch
        # Сдвиг окна СВОЙ на каждый день и на каждый аккаунт — поэтому
        # считаем его здесь, а не один раз на весь IP.
        segs = _day_segments(ch, rnd)
        base_out = outlier_segs
        # ── Первый день кампании (2026-08-22) ─────────────────────
        # Сессии, чьё время уже прошло, планировать бессмысленно —
        # dispatch выкинет их как просроченные. Раньше отсев делал
        # generate_daily_plan, и если у аккаунта не оставалось НИ ОДНОГО
        # будущего слота, он подставлял «сейчас + 15 минут». Для всех
        # аккаунтов IP это давало ОДНО И ТО ЖЕ время — прямое совпадение
        # (поймано E2E-тестом: acc#209 и acc#210 оба на 18:06).
        # Отсекаем здесь: планировщик видит всех соседей и разложит
        # остаток суток с соблюдением зазора.
        if not_before_min is not None:
            segs = _clip_segments(segs, not_before_min)
            base_out = _clip_segments(base_out, not_before_min)
            if not segs and not base_out:
                # До конца суток места нет вовсе — оставляем хвост дня,
                # пусть разложится хотя бы туда.
                segs = [(min(1380, not_before_min), 1440)]
                base_out = segs
            elif not segs:
                segs = base_out
        for _ in range(max(1, int(a.get("sessions", 5)))):
            # Изредка человек берёт телефон вне своего обычного режима.
            use = base_out if (base_out and rnd.random() < OUTLIER_CHANCE) else segs
            requests.append({"acc": a["id"], "segs": use})

    # Уже занятые слоты на этом IP (другие кампании / прошлые резервы)
    placed_all: list[int] = []
    for a in accounts:
        for t in (a.get("reserved") or []):
            placed_all.append(int(t))

    placed_self: dict = {a["id"]: [] for a in accounts}
    result: dict = {a["id"]: [] for a in accounts}

    # Порядок обработки случайный: иначе первые аккаунты стабильно
    # занимали бы лучшие места, и картина повторялась бы день ото дня.
    rnd.shuffle(requests)

    for req in requests:
        acc_id = req["acc"]
        segs = req["segs"]
        t = None
        for _ in range(_PLACE_ATTEMPTS):
            cand = _random_point(segs, rnd)
            if _fits(cand, placed_all, placed_self[acc_id], min_gap_min, self_gap_min):
                t = cand
                break
        if t is None:
            t = _best_free_slot(segs, placed_all, placed_self[acc_id],
                                min_gap_min, self_gap_min, rnd)
        if t is None:
            continue        # окно переполнено — сессию пропускаем
        t = max(0, min(1439, int(t)))
        placed_all.append(t)
        placed_self[acc_id].append(t)
        result[acc_id].append(t)

    return {
        acc_id: sorted((t // 60, t % 60) for t in times)
        for acc_id, times in result.items()
    }


def describe_chronotypes(accounts) -> dict:
    """{account_id: имя хронотипа} — для логов и UI."""
    return {a["id"]: assign_chronotype(a.get("phone", ""))["name"] for a in accounts}


def check_gaps(schedule: dict, min_gap_min: int = MIN_GAP_MIN) -> dict:
    """Проверка результата: минимальный фактический зазор на IP."""
    flat = []
    for acc_id, times in schedule.items():
        for (h, m) in times:
            flat.append((h * 60 + m, acc_id))
    flat.sort()
    worst = None
    violations = 0
    for i in range(1, len(flat)):
        gap = flat[i][0] - flat[i - 1][0]
        if worst is None or gap < worst:
            worst = gap
        if gap < min_gap_min:
            violations += 1
    return {
        "sessions": len(flat),
        "min_gap_actual": worst,
        "violations": violations,
        "required": min_gap_min,
    }
