# GramGPT — ТЗ: Человекоподобный комментинг v2

## Проблема

Текущий комментинг работает так:
1. Celery видит новый пост
2. Выбирает случайный аккаунт
3. Подключается → отправляет комментарий → отключается

Это **робот**. Telegram видит: аккаунт подключился, написал комментарий, отключился. Больше ничего не делал. Так 10 раз в день. Паттерн очевиден → бан.

## Решение: Комментарий = часть жизни аккаунта

Комментарий не должен быть отдельным действием. Он должен быть **спрятан внутри обычной активности** аккаунта, как у реального человека.

### Как это выглядит для реального человека:

```
11:23  Зашёл в телефон
11:23  Прочитал 3 чата
11:24  Посмотрел Stories
11:25  Открыл канал @crypto_news
11:25  Прочитал последний пост
11:26  Поставил 🔥
11:27  Прочитал ещё один пост
11:28  Написал комментарий: "а что думаете про ETH?"  ← ВОТ ОН
11:29  Пролистал ленту
11:30  Поставил реакцию на другой канал
11:31  Закрыл телефон
```

Комментарий — это одно из 10 действий за сессию. Не единственное.

---

## Архитектура: Behaviour Engine

### Концепция: у каждого аккаунта свой "характер"

При создании задачи прогрева+комментинга каждый аккаунт получает **персональный профиль поведения**:

```python
PERSONALITY_TEMPLATES = [
    {
        "name": "lurker",           # Молчун — много читает, мало пишет
        "comment_chance": 0.15,     # 15% шанс написать коммент за сессию
        "read_weight": 40,          # Много читает
        "reaction_weight": 15,
        "comment_delay_min": 600,   # Комментирует минимум через 10 мин после входа
        "comment_delay_max": 3600,  # Максимум через час
        "session_count": 2-3,       # Мало заходов в день
        "typing_before_comment": True,  # Печатает перед отправкой
        "reads_before_comment": 3-8,    # Читает 3-8 постов перед тем как написать
    },
    {
        "name": "active_reader",    # Активный читатель — часто заходит, иногда комментит
        "comment_chance": 0.35,
        "read_weight": 30,
        "reaction_weight": 25,
        "comment_delay_min": 180,
        "comment_delay_max": 1200,
        "session_count": 4-6,
        "typing_before_comment": True,
        "reads_before_comment": 2-5,
    },
    {
        "name": "commenter",        # Комментатор — часто пишет, но не спамер
        "comment_chance": 0.55,
        "read_weight": 20,
        "reaction_weight": 20,
        "comment_delay_min": 120,
        "comment_delay_max": 600,
        "session_count": 3-5,
        "typing_before_comment": True,
        "reads_before_comment": 1-3,
    },
    {
        "name": "reactor",          # Реактор — ставит реакции, редко пишет
        "comment_chance": 0.10,
        "read_weight": 20,
        "reaction_weight": 40,
        "comment_delay_min": 900,
        "comment_delay_max": 5400,
        "session_count": 3-4,
        "typing_before_comment": False,  # Импульсивный, сразу пишет
        "reads_before_comment": 1-2,
    },
    {
        "name": "night_owl",        # Ночной — активен вечером, утром спит
        "comment_chance": 0.30,
        "read_weight": 25,
        "reaction_weight": 20,
        "comment_delay_min": 300,
        "comment_delay_max": 1800,
        "session_count": 2-4,
        "active_hours": (14, 3),    # С 14:00 до 03:00
        "typing_before_comment": True,
        "reads_before_comment": 2-4,
    },
]
```

Каждый аккаунт получает **один** характер при создании задачи (детерминированно по хешу phone). Характер НЕ меняется между днями.

---

## Новый процесс комментирования

### Шаг 1: Комментирование встроено в прогрев

Сейчас прогрев и комментинг — отдельные системы. **Нужно объединить.** Комментарий — это одно из действий сессии прогрева.

```
Сессия аккаунта "lurker" (вечерняя, 12 действий):
  1. read_feed (канал @news)
  2. read_feed (чат "работа")
  3. view_stories
  4. read_feed (канал @crypto — ЦЕЛЕВОЙ)     ← прочитал целевой канал
  5. set_reaction (🔥 на пост в @crypto)     ← поставил реакцию
  6. read_feed (канал @memes)
  7. view_profile (друг)
  8. typing (Saved Messages)
  9. ★ COMMENT (@crypto, пост #1234)          ← комментарий спрятан среди действий
  10. read_feed (канал @tech)
  11. forward_saved (пост из @tech)
  12. send_saved ("ок")
```

### Шаг 2: Предварительное чтение целевого канала

Перед комментарием аккаунт ОБЯЗАН:

1. **Зайти в канал** — `get_messages(channel, limit=5)` + `send_read_acknowledge`
2. **Подождать** — 30-180с (имитация чтения поста)
3. **Иногда поставить реакцию** — 40% шанс поставить реакцию на пост перед комментированием
4. **Начать печатать** — `SetTypingAction` в discussion group на 3-8 секунд
5. **Отправить комментарий**
6. **Прочитать ещё 1-2 поста** — не уходить сразу после комментария

```python
async def _do_smart_comment(client, account, channel_username, post_id, comment_text, personality):
    """Человекоподобное комментирование — не просто send_message."""
    
    entity = await client.get_entity(channel_username)
    
    # 1. Прочитать последние посты (как будто листаем ленту)
    posts = await client.get_messages(entity, limit=random.randint(3, 8))
    for p in posts:
        await client.send_read_acknowledge(entity, p)
        await asyncio.sleep(random.uniform(1, 4))  # "Читаем" каждый пост
    
    # 2. Задержка — "читаем" целевой пост внимательно
    read_time = random.randint(30, 180)
    await asyncio.sleep(read_time)
    
    # 3. Иногда ставим реакцию перед комментарием (40%)
    if random.random() < 0.4:
        try:
            emoji = random.choice(["👍", "🔥", "❤️", "🤔", "👏"])
            await client(SendReactionRequest(
                peer=entity, msg_id=post_id,
                reaction=[ReactionEmoji(emoticon=emoji)]
            ))
            await asyncio.sleep(random.randint(5, 30))
        except:
            pass
    
    # 4. Typing перед комментарием
    if personality.get("typing_before_comment", True):
        discussion = await _get_discussion_group(client, entity)
        if discussion:
            typing_duration = random.randint(3, 12)  # Печатаем 3-12 секунд
            await client(SetTypingRequest(peer=discussion, action=SendMessageTypingAction()))
            await asyncio.sleep(typing_duration)
    
    # 5. Иногда НЕ отправляем (передумал) — 10% шанс
    if random.random() < 0.10:
        return "aborted", "Начал писать, передумал"
    
    # 6. Отправляем комментарий
    await client.send_message(entity=entity, message=comment_text, comment_to=post_id)
    
    # 7. После комментария — прочитать ещё 1-2 поста (не уходим сразу)
    # ВАЖНО: всё предварительное чтение (шаги 1-4) должно занимать 
    # НЕ БОЛЕЕ 2-3 минут. Основная задержка — в comment_queue.scheduled_at.
    # Нельзя чтобы "подготовка" съедала время пока пост горячий.
    await asyncio.sleep(random.randint(5, 20))
    more_posts = await client.get_messages(entity, limit=random.randint(1, 3))
    for p in more_posts:
        await client.send_read_acknowledge(entity, p)
        await asyncio.sleep(random.uniform(1, 3))
    
    return "ok", f"Комментарий отправлен в @{channel_username}"
```

### Шаг 3: Per-account лимиты и кулдауны

```python
ACCOUNT_LIMITS = {
    "max_comments_per_day": 3,          # Максимум 3 комментария в день
    "max_comments_per_channel_day": 1,  # Максимум 1 комментарий на канал в день
    "cooldown_after_comment_min": 120,  # Минимум 2 часа между комментариями
    "cooldown_after_comment_max": 360,  # Максимум 6 часов между комментариями
    "min_account_age_days": 3,          # Не комментировать первые 3 дня прогрева
}
```

**Нужна таблица `comment_cooldowns`:**
```sql
CREATE TABLE comment_cooldowns (
    account_id    INTEGER NOT NULL,
    channel       VARCHAR(128),
    last_comment  TIMESTAMP,
    comments_today INTEGER DEFAULT 0,
    day_reset_at  TIMESTAMP
);
```

### Шаг 4: Разные паттерны для разных аккаунтов

Нельзя чтобы все аккаунты комментировали одинаково. Для каждого аккаунта рандомизировать:

**Время реакции на новый пост:**
```python
# Аккаунт A: комментирует быстро (5-15 мин после поста)
# Аккаунт B: комментирует через час (40-90 мин)
# Аккаунт C: комментирует вечером (увидел утром, написал вечером)

# Старая логика была неправильной — комментарии через 1-4 часа бесполезны.
# Новая: 80% комментариев в первые 30 минут (пока пост горячий),
# 20% позже (для естественности).

COMMENT_TIMING_PROFILES = [
    # ── Быстрые (80% аккаунтов) — пока пост горячий ──
    {"name": "instant",   "delay_min": 45,   "delay_max": 180,  "weight": 15},  # 45с–3мин (самые первые)
    {"name": "fast",      "delay_min": 120,  "delay_max": 420,  "weight": 30},  # 2–7 мин
    {"name": "normal",    "delay_min": 300,  "delay_max": 900,  "weight": 25},  # 5–15 мин
    {"name": "careful",   "delay_min": 600,  "delay_max": 1800, "weight": 10},  # 10–30 мин

    # ── Поздние (20% аккаунтов) — для естественности ──
    {"name": "late",      "delay_min": 1800, "delay_max": 3600, "weight": 12},  # 30мин–1ч
    {"name": "very_late", "delay_min": 3600, "delay_max": 7200, "weight": 8},   # 1–2ч (увидел позже)
]

# Логика: чем быстрее комментарий — тем больше людей его увидят.
# Но если ВСЕ 50 аккаунтов напишут в первые 5 минут — подозрительно.
# Поэтому: основная масса (80%) в первые 30 мин с разбросом,
# остальные 20% позже — как люди которые зашли позднее.

**Стиль комментирования:**
```python
COMMENT_STYLE_PROFILES = [
    {
        "name": "short_responder",
        "length": "short",
        "uses_emoji": True,         # "огонь 🔥"
        "starts_with_reply": False, # Не цитирует пост
        "makes_typos": True,        # Иногда ошибки
    },
    {
        "name": "thinker",
        "length": "medium",
        "uses_emoji": False,
        "starts_with_reply": True,  # "Согласен с тем что..."
        "makes_typos": False,
    },
    {
        "name": "questioner",
        "length": "medium",
        "uses_emoji": False,
        "starts_with_reply": False,
        "asks_question": True,      # Всегда задаёт вопрос
        "makes_typos": True,
    },
]
```

### Шаг 5: Промпт для LLM с учётом стиля

```python
def build_comment_prompt(post_text, style_profile, personality):
    """Генерирует промпт для LLM с учётом стиля конкретного аккаунта."""
    
    base = "Ты — реальный пользователь Telegram. Напиши комментарий к посту."
    
    rules = []
    
    # Длина
    if style_profile["length"] == "short":
        rules.append("Пиши ОЧЕНЬ коротко: 2-5 слов максимум.")
    elif style_profile["length"] == "medium":
        rules.append("1-2 предложения, 20-80 символов.")
    else:
        rules.append("2-3 предложения, развёрнуто.")
    
    # Эмодзи
    if style_profile.get("uses_emoji"):
        rules.append("Можешь использовать 1-2 эмодзи, но не всегда.")
    else:
        rules.append("НЕ используй эмодзи.")
    
    # Ошибки
    if style_profile.get("makes_typos"):
        rules.append("Пиши разговорно, допускай мелкие ошибки (пропущенные запятые, 'чо' вместо 'что', 'норм' вместо 'нормально'). НЕ делай это в каждом слове — 1-2 ошибки максимум.")
    
    # Вопрос
    if style_profile.get("asks_question"):
        rules.append("Обязательно задай вопрос по теме поста.")
    
    # Цитирование
    if style_profile.get("starts_with_reply"):
        rules.append("Начни с реакции на конкретную мысль из поста ('Согласен с тем что...', 'Не уверен насчёт...', 'Интересная мысль про...').")
    
    # Общие правила
    rules.append("Пиши на том же языке что и пост.")
    rules.append("НЕ начинай с 'Отличный пост', 'Спасибо за информацию' — это шаблонно.")
    rules.append("Будь естественным. Представь что ты просто листаешь ленту и решил написать.")
    rules.append("Каждый комментарий должен быть УНИКАЛЬНЫМ. Никаких повторяющихся фраз.")
    
    prompt = f"""{base}

Правила для ЭТОГО комментария:
{chr(10).join(f'- {r}' for r in rules)}

Пост:
{post_text}

Напиши ТОЛЬКО текст комментария, ничего больше."""
    
    return prompt
```

---

## Новый флоу комментирования (полный)

```
1. run_periodic.py → каждые 90с → process_campaigns

2. Для каждой активной кампании:
   a. Веб-парсинг каналов (без Telethon) — находит новые посты
   b. Для каждого нового поста:
      
      i.   Проверяет триггер (all/random/keywords)
      ii.  Выбирает аккаунт (round-robin с рандомизацией, НЕ random.choice)
      iii. Проверяет лимиты аккаунта:
           - comments_today < max_comments_per_day?
           - last_comment + cooldown прошёл?
           - аккаунт прогревается >= min_account_age_days?
           - не комментировал этот канал сегодня?
      iv.  Если лимит — берёт следующий аккаунт
      v.   НЕ комментирует сразу! Ставит в очередь:
           
           comment_queue.append({
               "account_id": acc.id,
               "channel": channel.username,
               "post_id": post.post_id,
               "post_text": post.text,
               "scheduled_at": now + random_delay(personality),
               "personality": personality,
               "style": style_profile,
           })

3. Отдельный процесс (каждые 60с) проверяет очередь:
   - Есть ли задачи где scheduled_at <= now?
   - Если да:
     a. Подключает аккаунт через прокси
     b. Выполняет "предварительную активность":
        - read_feed (2-5 случайных каналов)
        - view_stories
        - set_reaction (случайный канал)
        - read целевой канал ← подготовка к комментарию
     c. Пауза 30-180с (читает пост)
     d. Typing 3-12с
     e. Отправляет комментарий
     f. Пост-активность:
        - read ещё 1-2 поста
        - иногда реакция
     g. Отключается
     h. Обновляет cooldown в БД
```

---

## Нужные изменения в БД

### Новая таблица: comment_queue
```sql
CREATE TABLE comment_queue (
    id            SERIAL PRIMARY KEY,
    campaign_id   INTEGER REFERENCES campaigns(id),
    account_id    INTEGER REFERENCES accounts(id),
    channel       VARCHAR(128) NOT NULL,
    post_id       INTEGER NOT NULL,
    post_text     TEXT,
    personality   JSONB,
    style         JSONB,
    status        VARCHAR(32) DEFAULT 'scheduled',  -- scheduled | executing | done | failed | aborted
    scheduled_at  TIMESTAMP NOT NULL,
    executed_at   TIMESTAMP,
    comment_text  TEXT,
    error         TEXT,
    created_at    TIMESTAMP DEFAULT NOW()
);
```

### Новая таблица: account_behavior
```sql
CREATE TABLE account_behavior (
    id              SERIAL PRIMARY KEY,
    account_id      INTEGER UNIQUE REFERENCES accounts(id),
    personality     VARCHAR(32) NOT NULL,   -- lurker/active_reader/commenter/reactor/night_owl
    timing_profile  VARCHAR(32) NOT NULL,   -- quick/normal/slow/delayed
    style_profile   JSONB NOT NULL,         -- short_responder/thinker/questioner
    comments_today  INTEGER DEFAULT 0,
    last_comment_at TIMESTAMP,
    day_reset_at    TIMESTAMP,
    channels_commented_today JSONB DEFAULT '[]',
    created_at      TIMESTAMP DEFAULT NOW()
);
```

### Изменения в warmup_tasks
```sql
ALTER TABLE warmup_tasks ADD COLUMN campaign_id INTEGER REFERENCES campaigns(id);
-- Связь прогрева с кампанией — комментарии встраиваются в сессии прогрева
```

---

## Метрики безопасности (KPI)

После реализации проверить:

| Метрика | Цель | Как проверить |
|---------|------|---------------|
| Комментов на аккаунт в день | ≤3 | Логи comment_queue |
| Подключений на аккаунт в день | ≤6 (3-4 сессии прогрева + 1-2 комментария) | Логи Telethon |
| Действий перед комментарием | ≥5 | Логи warmup_logs |
| Время от нового поста до комментария | 80% в первые 30 мин, 20% до 2ч | comment_queue.scheduled_at - post.created_at |
| Одинаковых комментариев | 0 | Проверка comment_queue.comment_text |
| Комментариев без подписки | 0 | Проверка subscribe_tasks |
| Комментариев без прокси | 0 | Проверка make_telethon_client |
| Дней прогрева перед первым комментарием | ≥3 | account_behavior.created_at vs first comment |
| Аккаунтов с одинаковым паттерном | 0 | Сравнение personality + timing + style |

---

## Приоритет реализации

### Фаза 1 (Critical — без этого банят)
1. Per-account лимиты (max 3 комментария/день)
2. Cooldown между комментариями (2-6 часов)
3. Не комментировать первые 3 дня прогрева
4. Предварительное чтение перед комментарием
5. Typing перед отправкой

### Фаза 2 (High — значительно снижает риск)
6. Персональные профили поведения (personality)
7. Разное время реакции на посты (timing profiles)
8. Comment queue вместо моментальной отправки
9. Round-robin ротация аккаунтов
10. Пост-активность после комментария

### Фаза 3 (Medium — дополнительная маскировка)
11. Объединение прогрева и комментинга в одну систему
12. Стили комментирования (short/thinker/questioner)
13. LLM промпты с учётом стиля
14. "Передумал" (10% шанс не отправить)
15. Контекстные цепочки действий

### Фаза 4 (Nice to have)
16. Автоматическое определение языка поста
17. Анализ контента перед комментированием (не комментировать рекламу)
18. Адаптивные лимиты (если аккаунт получил предупреждение — снизить активность)
19. A/B тестирование разных стратегий комментирования
