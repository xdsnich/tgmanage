# PostgreSQL Tuning для GramGPT (500-1000 юзеров)

## Зачем

С `NullPool` (см. utils/db_pool.py) каждый Celery-таск открывает свежее
соединение. При 40 параллельных тасках + FastAPI + диспетчер + Beat:

```
40 worker threads        × 1-2 conn = 40-80
FastAPI (uvicorn 4w)     × 5 conn   = 20
run_periodic / Beat                  = 2
                                     ────
Итого пик: ~100 соединений одновременно
```

Postgres дефолт `max_connections = 100` → упрётся в потолок при пиках.

## Применение миграции с индексами

```bash
cd api
python migrate.py            # применит миграцию 025
python migrate.py --status   # проверить что 025 применилась
```

Это безопасно — `CREATE INDEX IF NOT EXISTS` идемпотентно.
Если БД пустая или маленькая — мгновенно.
Если миллион строк — может занять несколько секунд (LOCK на чтение/запись).

## Тюнинг postgresql.conf

Найди файл:
```bash
# Windows (обычно)
C:\Program Files\PostgreSQL\16\data\postgresql.conf

# Linux
/etc/postgresql/16/main/postgresql.conf
```

Открой и измени параметры:

### Минимум (для 100 одновременных коннектов)

```ini
# Соединения
max_connections = 200              # было 100 — даём двойной запас
shared_buffers = 1GB               # было 128MB — кеш в RAM (~25% доступной памяти)
effective_cache_size = 3GB         # сколько RAM ОС использует под кеш PG (общая оценка)

# Производительность
work_mem = 16MB                    # на запрос (sort/hash). 16MB × 100 = 1.6GB пиковый
maintenance_work_mem = 256MB       # для VACUUM, CREATE INDEX
random_page_cost = 1.1             # для SSD (по дефолту 4.0, для HDD)

# WAL (write-ahead log)
wal_buffers = 16MB
checkpoint_completion_target = 0.9
max_wal_size = 2GB
min_wal_size = 512MB
```

### Для 8GB RAM machine с PG как единственным потребителем

```ini
max_connections = 300
shared_buffers = 2GB
effective_cache_size = 6GB
work_mem = 32MB
maintenance_work_mem = 512MB
```

### Для 16GB+ RAM

```ini
max_connections = 500
shared_buffers = 4GB
effective_cache_size = 12GB
work_mem = 64MB
maintenance_work_mem = 1GB
```

## После изменения — рестарт

```bash
# Windows (Services)
net stop postgresql-x64-16
net start postgresql-x64-16

# Linux
sudo systemctl restart postgresql
```

## Проверка что изменения применились

```sql
SHOW max_connections;       -- должно показать 200/300/500
SHOW shared_buffers;        -- 1GB / 2GB / 4GB
SHOW work_mem;              -- 16MB / 32MB / 64MB
```

## Мониторинг текущего использования

```sql
-- Сколько коннектов открыто прямо сейчас
SELECT count(*) FROM pg_stat_activity;

-- Кто держит коннекты
SELECT pid, usename, application_name, state, query
FROM pg_stat_activity
ORDER BY query_start;

-- Какие запросы самые медленные (требует pg_stat_statements)
SELECT query, calls, mean_exec_time, total_exec_time
FROM pg_stat_statements
ORDER BY mean_exec_time DESC
LIMIT 20;
```

## Когда переходить на PgBouncer

Если ты упёрся в `max_connections = 500` и хочешь больше — ставь PgBouncer.
Он мультиплексирует 1000+ клиентских коннектов в 30-50 реальных PG-коннектов
через transaction-mode pooling.

```ini
# pgbouncer.ini
[databases]
gramgpt = host=localhost port=5432 dbname=gramgpt

[pgbouncer]
listen_port = 6432
pool_mode = transaction         # КРИТИЧНО для нашего сценария
max_client_conn = 1000          # клиентов
default_pool_size = 25          # реальных коннектов на БД
```

И в .env меняешь:
```
DATABASE_URL=postgresql+asyncpg://gramgpt:gramgpt@localhost:6432/gramgpt
```

Но **сначала** хватит просто поднять max_connections в самом PG.
