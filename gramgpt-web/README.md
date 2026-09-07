# ufo.gram Web — Веб-дашборд

## 📁 Структура

```
gramgpt-web/
├── src/
│   ├── App.jsx                    ← роутинг
│   ├── main.jsx                   ← точка входа
│   ├── index.css                  ← глобальные стили
│   ├── components/
│   │   ├── ui/index.jsx           ← Button, Card, Input, Modal, Badge...
│   │   └── layout/Layout.jsx      ← сайдбар + структура
│   ├── pages/
│   │   ├── LoginPage.jsx
│   │   ├── RegisterPage.jsx
│   │   ├── DashboardPage.jsx      ← статистика, графики
│   │   ├── AccountsPage.jsx       ← таблица аккаунтов, CRUD
│   │   ├── ProxiesPage.jsx        ← управление прокси
│   │   ├── TasksPage.jsx          ← Celery задачи
│   │   └── SettingsPage.jsx       ← профиль, смена пароля
│   ├── services/
│   │   └── api.js                 ← все запросы к FastAPI
│   └── store/
│       └── authStore.js           ← Zustand: авторизация
├── index.html
├── vite.config.js                 ← прокси → localhost:8000
└── package.json
```

---

## 🚀 Установка и запуск — пошагово

### 1. Требования

- **Node.js 18+** → скачай с https://nodejs.org (кнопка LTS)
- Проверь: `node --version` и `npm --version`

### 2. Размести файлы

Скопируй папку `gramgpt-web/` в корень своего репозитория рядом с `api/` и `main.py`:

```
tg_manager/         ← твой корень
├── api/
├── main.py
├── gramgpt-web/    ← сюда кладёшь эту папку
│   ├── src/
│   ├── package.json
│   └── ...
```

### 3. Установи зависимости

```bash
cd gramgpt-web
npm install
```

Это создаст папку `node_modules/`. Займёт ~1 минуту.

### 4. Запусти FastAPI бэкенд

В отдельном терминале (из папки `api/`):

```bash
cd api
venv\Scripts\activate          # Windows
# или: source venv/bin/activate   # Linux/Mac
uvicorn main:app --reload --port 8000
```

Бэкенд должен быть на `http://localhost:8000`

### 5. Запусти фронтенд

```bash
# В папке gramgpt-web/
npm run dev
```

Откроется на `http://localhost:3000`

---

## ⚙️ Как работает связь фронт ↔ бэкенд

В `vite.config.js` настроен прокси:
- Фронтенд на порту 3000
- Все запросы `/api/*` автоматически идут на `localhost:8000`
- Это обходит CORS проблемы при разработке

---

## 🔧 Типичные ошибки и решения

### `npm install` падает с ошибкой сети
→ Проверь интернет. Попробуй: `npm install --legacy-peer-deps`

### `Cannot find module` при запуске
→ Убедись что ты находишься в папке `gramgpt-web/` когда запускаешь команды

### Белый экран / ошибки в консоли браузера
→ Открой DevTools (F12) → Console — там будет конкретная ошибка

### Запросы к API падают с 401
→ Зарегистрируйся через `/register`, потом войди

### Задачи не запускаются
→ Нужен запущенный Celery воркер (см. страницу Tasks в дашборде)

---

## 📦 Сборка для продакшена

```bash
npm run build
# Создаст папку dist/ — это готовый статичный сайт
# Можно раздавать через nginx или любой хостинг
```
