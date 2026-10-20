---
name: simulation-testing
description: >
  Имитест — автономное E2E-тестирование с самолечением.
  Visible-браузер (Playwright), умные селекторы, диагностика ошибок по SSH,
  сквозной отчёт. Без привязки к проектам.
category: software-development
tags:
  - e2e
  - qa
  - имитест
  - тестирование
  - visible
  - playwright
  - browser
  - автофикс
  - регрессия
  - универсальный
---

# 🧪 Имитест — универсальный E2E-тестер

Пользователь говорит «имитест» — агент сам разбирается в проекте и прогоняет его
видимым браузером. Скрипт: [`imitester.py`](imitester.py).

---

## ⚡ Режимы

### Автономный (по умолчанию)

```bash
python imitester.py --config project.json          # базовый прогон
python imitester.py --config project.json --deep   # + ссылки, адаптив, a11y
python imitester.py --config project.json --keep-open  # оставить окно, посмотреть глазами
```

### Коллаборативный (парное тестирование)

Браузер остаётся открытым, агент проходит по элементам и спрашивает о неочевидном:

```
Ты: «запускай коллаборативный»
Я:  логин ✅, дашборд ✅, settings → форма съехала?
Ты: «баг, чини»
Я:  правлю → retest → продолжаю
```

### Классические режимы

- **Mode B** — презентационный: один workflow целиком.
- **Mode C** — полный цикл: все разделы, состояния, ошибки.
- **Deep** — качество: ссылки, адаптив, a11y, производительность.

---

## 🧭 Пайплайн агента (адаптация под проект)

Тестирование настраивается **агентом**, а не пользователем:

1. **Понять проект** — код/CodeGraph, README, роутер фронтенда, список страниц.
2. **Определить вход** — где логин, какие селекторы, есть ли 2FA/CAPTCHA.
3. **Собрать конфиг** — `base_url`, `login.steps`, `sections`, `screenshot_dir`.
4. **Прогнать** — visible-браузер, базовый режим; смотреть снимки и `report.json`.
5. **Углубить** — `--deep`, ожидание стриминга (`settle_ms`), повторные проходы.
6. **Локализовать** — по 5xx: SSH → журнал → классификация → `grep` кода.
7. **Отчитаться** — passed / warnings / failed + что требует человека.

---

## 🚨 Критическое правило: visible, НЕ headless

| Что получаю | Headless (snapshot) | Visible Playwright |
|---|---|---|
| DOM | Accessibility Tree (урезан) | **Полный DOM, любые селекторы** |
| Network | только 4xx/5xx в консоли | **Перехват всех ответов** |
| Console | по запросу | **В реальном времени, все уровни** |
| pageerror | нет | **`page.on("pageerror")`** |
| State | нет | **localStorage, cookie, React state** |
| Скриншоты | нет | **есть** |

Visible — не для демонстрации. Visible — чтобы видеть всё, что делает приложение.
`--headless` допустим только в CI, где смотреть некому.

### ⚠️ Не закрывай браузер слишком рано

Ответ AI-агента формируется стримингом и может идти 10–90 секунд.

Антипаттерн (ложноположительный тест):

```python
page.wait_for_timeout(2000)
body = page.locator("body").inner_text()
if "погода" in body:      # слово есть в сообщении ОБ ОШИБКЕ
    print("✅ passed")    # ложь
```

Правильно:

1. Ждать **стабилизации** — 5+ секунд без изменения длины текста (`wait_for_stable`).
2. Проверять **отсутствие** маркеров ошибки: «к сожалению», «возникла проблема», «недоступен».
3. Проверять **наличие** конкретных фактов, которые мог дать только реальный ответ
   (цифры, даты, имена).
4. Максимум ожидания — 60–90 секунд (`settle_ms`).
5. Если ответ неполный — **не закрывать браузер**, дать человеку посмотреть (`--keep-open`).
6. Скриншот полного ответа: `page.screenshot(full_page=True)`.

---

## 🧨 Эксперименты на рабочей машине

Тест — не повод лишать пользователя связи. Правила, когда прогон трогает СИСТЕМУ
(DNS, маршруты, адаптеры, фаервол, VPN-клиент):

1. **Сначала unit-тесты, потом машина.** Логику проверять без живого изменения системы.
2. **Откат готов ДО изменения.** Одна команда должна возвращать всё назад.
3. **Не убивать процессы и не менять сетевые настройки без крайней нужды.**
   Краш-сценарии проверяются тестами, а не `taskkill /F` на боевой сессии.
4. **После каждого изменения — контроль связности:** `curl -m 8 -o /dev/null -w '%{http_code}'`.
5. **Снимок состояния — на диск, не в память.** Клиент могут убить в любой момент.

Симптом провала: «опять интернет пропал» — значит правила нарушены: откатывать, не объяснять.

---

## 🏗 Архитектура `imitester.py`

```
imitester.py
├── Config        — JSON + дефолты, deep-copy шаблона
├── Playwright    — visible chromium (headless по флагу)
│   ├── page.on("response")  — 4xx/5xx
│   ├── page.on("console")   — console.error (без favicon-шума)
│   └── page.on("pageerror") — непойманные исключения
├── Login         — шаги fill / click / wait_url / wait_text / wait_ms
├── wait_for_stable — ожидание стабилизации текста (стриминг)
├── SmartSelector — 7 стратегий поиска элемента
├── Checks        — inputs, buttons, links, a11y, адаптив
├── SSHDiagnostics — journalctl + grep (read-only), рестарт по флагу
└── Report        — JSON + скриншоты, коды возврата 0/1/2
```

---

## ⚙️ Конфигурация

```json
{
  "name": "Проект",
  "base_url": "https://app.example.com",
  "viewport": {"width": 1440, "height": 900},
  "screenshot_dir": "imitester_screenshots",
  "timeout_ms": 20000,
  "login": {
    "enabled": true,
    "url": "/login",
    "steps": [
      {"fill": "input[name='email']", "value": "user@example.com"},
      {"fill": "input[name='password']", "value_env": "IMITEST_PASSWORD"},
      {"click": "button[type='submit']"},
      {"wait_url": "**/dashboard*"}
    ]
  },
  "sections": [
    {"name": "home", "url": "/"},
    {"name": "chat", "url": "/chat", "settle_ms": 60000}
  ],
  "skip_click_regex": "удал|delete|выход|logout|оплатить|pay",
  "ssh": {"enabled": false, "host": "", "user": "", "key": "~/.ssh/id_ed25519",
          "sudo": false, "allow_restart": false},
  "services": {"backend": "systemctl restart backend"},
  "project_path": "/opt/app"
}
```

Правила:

- Пароль — только через `value_env`, никогда в файле/репозитории.
- `sections[].url` — относительный путь от `base_url` (склейка через `urljoin`).
- `settle_ms` — бюджет ожидания стабилизации страницы (для AI-чатов 60000–90000).
- `ssh.enabled: false` — обычный режим: 5xx помечаются как `failed` без диагностики.

---

## 🔍 SmartSelector

Перебирает стратегии, пока не найдёт элемент:

```python
strategies = [role button, role link, role heading,
              button:has-text, a:has-text, :has-text, textbox]
```

- `count()` — только с учётом API: в sync — без `await`, в async — с `await`.
- **Strict mode violation** (два элемента с одним текстом) → `.first`.
- **Не найден** → WARNING, не FAIL: UI мог измениться намеренно.

---

## 🔧 Диагностика 5xx (SSH, read-only)

```
500 /api/...
  ↓ SSH → journalctl → ERROR
  ↓ классификация: MissingGreenlet / ImportError / AttributeError / ...
  ↓ grep кода → файл и первая строка совпадения
  ↓ (опционально) restart при ssh.allow_restart=true → retest
```

Автоматических правок кода тестер не делает: `sed` по живому проекту — это отдельное,
осознанное действие агента с бэкапом. Source of truth — код на сервере, не локальная копия.

---

## 📋 Сквозной цикл (8 уровней)

| Level | Что проверяется |
|---|---|
| 1 | Загрузка раздела + стабилизация контента + скриншот |
| 2 | Сбор элементов: inputs, buttons, links, images |
| 3 | Inputs: disabled, label/`aria-label`/placeholder |
| 4 | Buttons: клик, реакция, возврат на страницу, пропуск опасных |
| 5 | Сеть: 4xx/5xx (`page.on("response")`) |
| 6 | JS: `console.error`, `pageerror` |
| 7 | Deep: битые ссылки, адаптив 390/768/1440, `img` без `alt`, `h1`, `<title>` |
| 8 | Локализация 5xx через SSH-диагностику |

---

## 🐛 Грабли (проект-независимые)

| Проблема | Причина | Решение |
|---|---|---|
| Прогон падает на первой кнопке | `page.goto(...).__exit__(...)` в старом шаблоне | Просто `page.goto(...)` (баг версии 1.x) |
| Все разделы «не загрузились» | `page.goto("/path")` — относительный URL | Склеивать с `base_url` (`urljoin`) |
| `wait_url` таймаутит, хотя редирект был | Glob не покрывает query-string | `**/dashboard*`, а не `**/dashboard` |
| «стабильно через 0 c» на любой странице | `len(page.evaluate("...length"))` → `TypeError` в `try/except` | `int(...)` без `len()`; не глотать исключение молча |
| В отчёте десятки `favicon` 404 | Консольный дубль сетевой ошибки | Фильтровать «Failed to load resource» и favicon |
| Selector не найден | DOM изменился | SmartSelector → WARNING |
| `count()` всегда 0 | Забыт `await` в async-API | `await page.locator(...).count()` |
| Error Boundary глотает ошибку | React/Next в headless | Только visible Playwright |
| Клик уводит со страницы | Кнопка — ссылка/сабмит | Возврат на `saved_url` после клика |
| Ложный «✅» в AI-чате | 2 секунды ожидания на стриминге | `wait_for_stable`, 60–90 с |
| Правка кода через `sed` «на лету» | Автофикс без бэкапа | Только диагностика; правка — отдельным шагом |
| Bearer-токен не работает из терминала | Маскирование секретов в обвязке агента | Выполнять API-запрос на сервере |

---

## 📊 Отчёт

```json
{
  "timestamp": "2026-10-20 11:01:01",
  "config_name": "Проект",
  "base_url": "https://app.example.com",
  "deep": true,
  "total_elements": 184,
  "passed": 31, "auto_fixed": 0, "warnings": 3, "failed": 2,
  "duration_sec": 17,
  "console_errors": [{"section": "chat", "text": "TypeError: x is undefined"}],
  "network_errors": [{"section": "chat", "status": 500, "url": "…/api/v1/messages"}],
  "page_errors": [],
  "results": [{"section": "home", "element": "button Войти", "status": "passed"}],
  "screenshots_dir": "…/imitester_screenshots"
}
```

Коды возврата: `0` — чисто, `1` — есть `failed`, `2` — тестер не смог запуститься.
Флаг `--json` печатает итог одной JSON-строкой для CI/автоматизации.

---

## ✅ Самопроверка и поддержка

```bash
python selftest/run_selftest.py
```

Стенд поднимает локальный сайт с заложенными дефектами и проверяет, что тестер их видит
(логин, `pageerror`, 404, `console.error`, `img` без `alt`, битая ссылка, скриншоты).
Самопроверка падает первой, если тестер «слепнет» — прогоняй её после любой правки
`imitester.py`.

Правила поддержки:

- Один фикс — один прогон самопроверки, потом боевой проект.
- Новые проверки добавлять так, чтобы они не превращались в шум (иначе отчёт перестают читать).
- Разрушающие действия и рестарты — только по явному флагу в конфиге.

---

## 🚀 Быстрый старт

```bash
pip install playwright paramiko
playwright install chromium

cp imitester.example.json project.json     # поправить base_url/login/sections
python imitester.py --config project.json --deep
cat imitester_screenshots/report.json
```
