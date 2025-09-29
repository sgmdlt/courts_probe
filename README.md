# Courts Probe

Минимальный мониторинг доступности сайтов судов: FastAPI-приложение с фоновой задачей и SQLite для списка адресов.

## Как это работает

1. Скрипт `get_courts.py` асинхронно скачивает актуальные ссылки с sudrf.ru, использует прокси из файла `proxy`, сохраняет результат в `courts.csv` и таблицу `courts` в `data/monitor.sqlite`.
2. `app.py` на старте поднимает фон на asyncio: берёт URL из базы, делает до 5 попыток запроса (HEAD с fallback на GET), кеширует свежие статусы в памяти.
3. HTML-дашборд (`/`) отдаёт текущие статусы, `/health` — проверку живости. Шаблоны и стили лежат в `app/templates` и `app/static`.

## Подготовка окружения

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

Файл `proxy` должен содержать прокси в формате `user:pass@host:port` (по одному в строке). При необходимости добавьте `http://` — скрипт делает это автоматически.

## Получение списка судов

```bash
python get_courts.py
```

После выполнения получите:
- `courts.csv` — плоский CSV для ручного просмотра;
- `data/monitor.sqlite` — таблицу `courts(code TEXT PRIMARY KEY, url TEXT)`.

## Запуск приложения

Локально:
```bash
uvicorn app:app --reload
```

Через Docker Compose:
```bash
docker compose up --build
```

UI доступен на http://localhost:8000. База и CSV монтируются из хоста, чтобы фоновые проверки автоматически подхватывали обновления списка.

## Настройки

Параметры фона заданы в `app.py` (интервал опроса, таймаут, число ретраев). При необходимости правьте константы `CHECK_INTERVAL_SECONDS`, `REQUEST_TIMEOUT_SECONDS`, `MAX_ATTEMPTS`, `RETRY_DELAY_SECONDS`, `SUCCESS_STATUS_CODES` и `USER_AGENT`.

## Разработка

- Проверка синтаксиса: `python -m compileall app.py get_courts.py`.
- Для расширений добавьте тесты (`pytest`) или линтер (`ruff`) из набора `pip install .[dev]`.
