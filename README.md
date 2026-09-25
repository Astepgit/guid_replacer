# Замена GUID в XML-выгрузке 1С

Веб-приложение для замены GUID в XML-файлах выгрузки 1С с автоматической синхронизацией справочников через HTTP-сервис.

## Возможности

- **Загрузка и парсинг XML** — извлечение всех объектов метаданных (справочники, документы, регистры и т.д.).
- **Синхронизация с 1С** — получение актуальных GUID из базы 1С через HTTP-сервис с потоковым прогрессом (SSE).
- **Автоматическая замена GUID** — по совпадению типа, наименования и признака группы объекта.
- **Генерация изменённого XML** — с сохранением исходной структуры, пробелов и пространств имён.
- **Кодировка UTF-8 with BOM** — совместимость с 1С.
- **Безопасность** — XSS-защита, потокобезопасные сессии, конфигурация через `.env`.
- **Производительность** — WAL-режим SQLite, батч-вставка, индекс по `(type, name, is_folder)`, XPath-выборка.
- **Доступность** — ARIA-роли, keyboard navigation, контрасты по WCAG AA.

## Требования

- Python 3.9+
- Доступ к HTTP-сервису 1С (Basic Auth)
- Установленные зависимости (см. `requirements.txt`)

## Установка

1. Клонируйте репозиторий:
   ```bash
   git clone https://github.com/Astepgit/guid_replacer.git
   cd guid_replacer
   ```

2. Установите зависимости:
   ```bash
   pip install -r requirements.txt
   ```
   Для разработки (тесты, линтеры):
   ```bash
   pip install -r requirements-dev.txt
   ```

3. Создайте файл `.env` на основе `.env.example` и укажите:
   ```env
   MCP_USER=your_1c_login
   MCP_PASSWORD=your_strong_password
   MCP_QUERY_URL=http://your-1c-server/base/hs/mcp-1c/query

   # Опциональные
   FLASK_HOST=127.0.0.1
   FLASK_PORT=5000
   FLASK_DEBUG=false
   LOG_LEVEL=INFO
   LOG_FILE=app.log
   DB_PATH=guid_cache.db
   UPLOAD_FOLDER=uploads
   MAX_CONTENT_LENGTH=104857600
   SESSION_TTL=3600
   ```

4. (Опционально) Если используете MCP-серверы (`config.yaml`), скопируйте `config.example.yaml` в `config.yaml` и заполните секреты:
   ```bash
   cp config.example.yaml config.yaml
   # Отредактируйте config.yaml, заменив ${MCP_CODEPILOT_TOKEN} и ${MCP_1C_USER}/${MCP_1C_PASSWORD}
   ```

## Запуск

```bash
python3 app.py
```

После запуска сервер будет доступен по адресу `http://127.0.0.1:5000` (по умолчанию).

> ⚠️ **Безопасность:** по умолчанию сервер слушает только `127.0.0.1`. Для доступа из сети установите `FLASK_HOST=0.0.0.0` **и** поставьте сервер за reverse proxy с аутентификацией (nginx + Basic Auth).

## Использование

1. **Загрузите XML-файл** — перетащите файл в область загрузки или нажмите «Выбрать файл», затем «Загрузить».
2. **Синхронизируйте базу GUID** — вкладка «База GUID (1С)» → кнопка «Синхронизировать».
   - Наблюдайте за прогрессом в реальном времени (SSE-поток).
   - Кнопка блокируется на время синхронизации, чтобы избежать гонок.
3. **Выполните автозамену GUID** — вкладка «Объекты из XML» → кнопка «Автозамена».
   - Подсчитываются статистики: заменено / не найдено / уже совпадали / без описания.
   - Заменённые строки подсвечиваются жёлтым.
4. **Просмотрите замены** — вкладка «Замены GUID» показывает старый → новый GUID с типом, описанием, поиском.
5. **Скачайте изменённый XML** — кнопка «Скачать XML».
   - Файл сохраняется с суффиксом `_modified` в кодировке UTF-8 with BOM.

## Тестирование

```bash
# Установка dev-зависимостей
pip install -r requirements-dev.txt

# Запуск тестов
pytest tests/ -v

# Запуск с покрытием
pytest tests/ --cov=app --cov=parsing --cov=sync_db --cov=db --cov-report=term-missing

# Линтер
ruff check .

# Форматирование
black --check .
```

CI запускается автоматически на GitHub Actions для Python 3.9–3.12.

## Структура проекта

```
guid_replacer/
├── app.py                 # Flask-сервер, маршруты, _sessions (потокобезопасно)
├── parsing.py             # Общий модуль: parse_xml, replace_guids_in_xml, CATEGORY_MAP
├── sync_db.py             # Синхронизация с 1С (in-process run_sync, без subprocess)
├── db.py                  # init_db, get_db_connection, load_guid_index (WAL-режим)
├── parser.py              # CLI-утилита → objects.json
├── test_connection.py     # Exploratory тест подключения к 1С
├── templates/
│   └── index.html         # Веб-интерфейс с ARIA, escapeHtml, toast-уведомлениями
├── tests/
│   ├── conftest.py        # Fixtures: изолированное окружение, мок 1С
│   ├── fixtures/
│   │   └── sample.xml     # Тестовый XML-файл
│   ├── test_parsing.py    # Покрытие parsing.py
│   ├── test_app.py        # Интеграционные тесты Flask-маршрутов
│   └── test_sync_db.py    # Тесты синхронизации с мок-сервером 1С
├── scripts/
│   └── test_e2e.py        # End-to-end smoke-тест
├── .github/workflows/
│   └── ci.yml             # GitHub Actions CI
├── .pre-commit-config.yaml  # ruff + black + pytest hooks
├── pyproject.toml         # Конфиг ruff, black, pytest, mypy
├── config.example.yaml    # Шаблон MCP-конфига (без секретов!)
├── .env.example           # Шаблон .env
├── .env                   # Ваши секреты (в .gitignore)
├── .gitignore
├── requirements.txt       # Зависимости (зафиксированы версии)
├── requirements-dev.txt   # Зависимости для разработки
├── KODA.md                # История TODO по раундам
└── README.md              # Этот файл
```

## Технологии

- **Flask 2.3+** — веб-фреймворк.
- **lxml 4.9+** — парсинг и генерация XML с сохранением структуры.
- **SQLite (WAL)** — локальное хранение GUID с конкурентным чтением во время записи.
- **Server-Sent Events (SSE)** — потоковый прогресс синхронизации с heartbeat.
- **pytest** — unit- и интеграционные тесты с мок-сервером 1С.
- **ruff + black** — линтинг и форматирование.

## Безопасность

- Все секреты — в `.env`, не в коде. `config.yaml` исключён из git.
- XSS-защита: все данные из 1С/XML экранируются через `escapeHtml()` перед вставкой в `innerHTML`.
- `_sessions` защищены `threading.Lock` — нет race condition при конкурентных запросах.
- Временные файлы (`modified.xml`) удаляются через `after_request` — нет утечек на диске.
- По умолчанию `host=127.0.0.1` — сервер доступен только на локальной машине.

## Устранение ошибок

### Ошибка XDTO при загрузке в 1С
- Убедитесь, что установлен `lxml`.
- Проверьте, что файл сохранён в кодировке UTF-8 with BOM (используется автоматически).
- Если ошибка сохраняется, попробуйте загрузить файл через другой XML-редактор (например, Notepad++) и сохранить с BOM.

### Ошибка подключения к 1С
- Проверьте доступность URL, указанного в `MCP_QUERY_URL`.
- Убедитесь, что логин и пароль в `.env` верны.
- Проверьте, что HTTP-сервис 1С опубликован и работает.
- Для отладки используйте `python test_connection.py` (требует явного URL).

### Синхронизация прерывается
- Проверьте стабильность соединения с 1С (SSE переподключается через 5 секунд при обрыве).
- При долгой синхронизации (>10 минут) увеличьте `SYNC_HTTP_TIMEOUT` в `.env`.
- Смотрите логи в `app.log`.

## Лицензия

Проект предоставляется «как есть» без каких-либо гарантий.
