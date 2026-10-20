#!/usr/bin/env python3
"""imitester.py — автономный E2E-тестер с самолечением.

Видимый (visible) Playwright, самовосстанавливающиеся селекторы,
перехват сети/консоли, диагностика через SSH, отчёт в JSON.

Запуск:
  python imitester.py                          # базовый прогон
  python imitester.py --deep                   # + адаптив, a11y, проверка ссылок
  python imitester.py --config project.json    # конфигурация проекта
  python imitester.py --headless               # без окна (CI)

Требования:
  pip install playwright paramiko
  playwright install chromium

Коды возврата: 0 — нет ошибок, 1 — есть failed, 2 — критическая ошибка запуска.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sys
import time
import traceback
from dataclasses import asdict, dataclass, field
from typing import Any, Optional
from urllib.parse import urljoin

# ═══════════════════════════════════════════════
# КОНФИГУРАЦИЯ ПО УМОЛЧАНИЮ (шаблон — правьте под проект)
# ═══════════════════════════════════════════════

DEFAULT_CONFIG: dict[str, Any] = {
    "name": "Мой проект",
    "base_url": os.environ.get("IMITEST_URL", "http://localhost:3000"),
    "viewport": {"width": 1440, "height": 900},
    "screenshot_dir": "imitester_screenshots",
    "timeout_ms": 20000,
    "slow_mo_ms": 0,
    # Логин выполняется один раз перед обходом разделов.
    # Пароль НЕ храните в репозитории — берите из окружения (value_env).
    "login": {
        "enabled": False,
        "url": "/login",
        "steps": [
            {"fill": "input[name='email']", "value": "user@example.com"},
            {"fill": "input[name='password']", "value_env": "IMITEST_PASSWORD"},
            {"click": "button[type='submit']"},
            {"wait_url": "**/dashboard*"},
        ],
    },
    "sections": [
        {"name": "home", "url": "/"},
        {"name": "about", "url": "/about"},
    ],
    # Кнопки, которые не нажимаем автоматически (разрушающие действия).
    "skip_click_regex": "удал|delete|выход|logout|sign out|оплатить|pay|опубликовать|publish",
    # Диагностика через SSH: только чтение логов/кода + опциональный рестарт.
    "ssh": {
        "enabled": False,
        "host": "",
        "user": "",
        "key": "~/.ssh/id_ed25519",
        "port": 22,
        "sudo": False,
        "allow_restart": False,
    },
    "services": {},
    "project_path": "",
    # Учётные данные HTTP-авторизации (если сайт за basic auth).
    "http_credentials": None,
}


# ═══════════════════════════════════════════════
# МОДЕЛИ ОТЧЁТА
# ═══════════════════════════════════════════════

@dataclass
class TestResult:
    section: str
    element: str
    status: str  # passed | warning | failed | auto_fixed
    error: Optional[str] = None
    auto_fix: Optional[str] = None
    screenshot: Optional[str] = None
    duration_ms: int = 0


@dataclass
class TestReport:
    timestamp: str = ""
    config_name: str = ""
    base_url: str = ""
    deep: bool = False
    total_elements: int = 0
    passed: int = 0
    auto_fixed: int = 0
    warnings: int = 0
    failed: int = 0
    duration_sec: int = 0
    screenshots_dir: str = ""
    console_errors: list = field(default_factory=list)
    network_errors: list = field(default_factory=list)
    page_errors: list = field(default_factory=list)
    results: list = field(default_factory=list)

    def recount(self) -> None:
        self.passed = sum(1 for r in self.results if r.status == "passed")
        self.auto_fixed = sum(1 for r in self.results if r.status == "auto_fixed")
        self.warnings = sum(1 for r in self.results if r.status == "warning")
        self.failed = sum(1 for r in self.results if r.status == "failed")


# ═══════════════════════════════════════════════
# SMART SELECTOR
# ═══════════════════════════════════════════════

class SmartSelector:
    """Ищет элемент по 7 стратегиям: role → has-text → textbox → ..."""

    STRATEGIES = (
        ("role_button", lambda p, t: p.get_by_role("button", name=t)),
        ("role_link", lambda p, t: p.get_by_role("link", name=t)),
        ("role_heading", lambda p, t: p.get_by_role("heading", name=t)),
        ("button_text", lambda p, t: p.locator(f"button:has-text({_q(t)})")),
        ("link_text", lambda p, t: p.locator(f"a:has-text({_q(t)})")),
        ("any_text", lambda p, t: p.locator(f":has-text({_q(t)})")),
        ("textbox", lambda p, t: p.get_by_role("textbox", name=t)),
    )

    @classmethod
    def find(cls, page, text: str, role: Optional[str] = None):
        text = (text or "").strip()
        if not text:
            return None
        strategies = list(cls.STRATEGIES)
        if role:
            strategies.insert(0, ("exact_role", lambda p, t: p.get_by_role(role, name=t)))
        for _, fn in strategies:
            try:
                loc = fn(page, text)
                if loc.count() > 0:          # sync API: count() без await
                    return loc.first
            except Exception:
                continue
        return None


def _q(text: str) -> str:
    """Кавычки для CSS/Playwright-селекторов."""
    return '"' + text.replace('"', '\\"').replace("\n", " ") + '"'


# ═══════════════════════════════════════════════
# SSH-ДИАГНОСТИКА (только чтение по умолчанию)
# ═══════════════════════════════════════════════

class SSHDiagnostics:
    """Читает логи/код на сервере. Рестарт сервиса — только если allow_restart."""

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.client = None

    @property
    def enabled(self) -> bool:
        s = self.cfg.get("ssh") or {}
        return bool(s.get("enabled") and s.get("host") and s.get("user"))

    def connect(self):
        import paramiko

        s = self.cfg["ssh"]
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(
            s["host"],
            port=int(s.get("port", 22)),
            username=s["user"],
            key_filename=os.path.expanduser(s["key"]),
            timeout=10,
        )

    def run(self, cmd: str) -> str:
        if not self.client:
            self.connect()
        _, stdout, stderr = self.client.exec_command(cmd, timeout=30, get_pty=True)
        out = stdout.read().decode(errors="replace").strip()
        return out or stderr.read().decode(errors="replace").strip()

    def sudo_run(self, cmd: str) -> str:
        return self.run(f"sudo bash -c {_q(cmd)}") if self.cfg["ssh"].get("sudo") else self.run(cmd)

    def get_logs(self, service: str, lines: int = 50) -> str:
        return self.sudo_run(f"journalctl -u {service} --no-pager -n {lines}")

    def grep_code(self, pattern: str, path: Optional[str] = None) -> str:
        root = path or self.cfg.get("project_path") or "."
        return self.run(
            f"grep -rn {_q(pattern)} {_q(root)} "
            f"--include='*.py' --include='*.ts' --include='*.tsx' | head -20"
        )

    def restart_service(self, service: str) -> str:
        if not self.cfg["ssh"].get("allow_restart"):
            return "рестарт запрещён (ssh.allow_restart=false)"
        return self.sudo_run(self.cfg.get("services", {}).get(service, f"systemctl restart {service}"))

    @staticmethod
    def classify_error(log_text: str) -> str:
        patterns = {
            "MissingGreenlet": r"MissingGreenlet",
            "ImportError": r"ImportError.*No module named",
            "AttributeError": r"AttributeError",
            "TypeError": r"TypeError",
            "SyntaxError": r"SyntaxError",
            "PermissionError": r"PermissionError|Forbidden",
            "ConnectionError": r"ConnectionError|Connection refused",
            "Timeout": r"TimeoutError|timed out",
        }
        for name, pattern in patterns.items():
            if re.search(pattern, log_text):
                return name
        return "Unknown"


# ═══════════════════════════════════════════════
# ОСНОВНОЙ ТЕСТЕР
# ═══════════════════════════════════════════════

class Imitester:
    def __init__(self, config: dict[str, Any], deep: bool = False,
                 headless: bool = False, keep_open: bool = False):
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # глубокая копия шаблона
        cfg.update(config)
        self.config = cfg
        self.deep = deep
        self.headless = headless
        self.keep_open = keep_open
        self.base_url = cfg["base_url"].rstrip("/")
        self.timeout = int(cfg.get("timeout_ms", 20000))
        self.shot_dir = cfg["screenshot_dir"]
        os.makedirs(self.shot_dir, exist_ok=True)

        self.report = TestReport(
            timestamp=_dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            config_name=cfg.get("name", ""),
            base_url=self.base_url,
            deep=deep,
            screenshots_dir=os.path.abspath(self.shot_dir),
        )
        self.ssh = SSHDiagnostics(cfg)
        self.browser = None
        self.context = None
        self.page = None
        self.section = "-"
        self.shot_num = 0
        self.section_errors: list[dict] = []
        self.started = time.time()

    # ── служебное ──────────────────────────────

    def log(self, msg: str) -> None:
        print(f"[{_dt.datetime.now():%H:%M:%S}] {msg}", flush=True)

    def add(self, element: str, status: str, error: str = None,
            auto_fix: str = None, shot: str = None, ms: int = 0) -> None:
        self.report.results.append(
            TestResult(self.section, element, status, error, auto_fix, shot, ms)
        )

    def screenshot(self, name: str, full_page: bool = False) -> Optional[str]:
        self.shot_num += 1
        safe = re.sub(r"[^\w\-.]+", "_", name)[:50]
        path = os.path.join(self.shot_dir, f"{self.shot_num:02d}_{safe}.png")
        try:
            self.page.screenshot(path=path, full_page=full_page)
            return path
        except Exception:
            return None

    def abs_url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return urljoin(self.base_url + "/", path.lstrip("/"))

    def wait_for_stable(self, min_idle_ms: int = 3000, timeout_ms: int = 60000) -> int:
        """Ждёт стабилизации текста страницы (для стриминговых ответов AI-агентов).

        Возвращает время стабилизации в секундах. Контент считается полным,
        когда длина текста не меняется min_idle_ms подряд.
        """
        t0 = time.time()
        last_len, idle_since = -1, time.time()
        while (time.time() - t0) * 1000 < timeout_ms:
            try:
                size = int(self.page.evaluate(
                    "() => document.body ? document.body.innerText.length : 0"))
            except Exception as exc:
                self.log(f"  ⚠️ стабилизация контента недоступна: {exc}")
                break
            if size != last_len:
                last_len, idle_since = size, time.time()
            elif (time.time() - idle_since) * 1000 >= min_idle_ms:
                break
            self.page.wait_for_timeout(250)
        return int(time.time() - t0)

    # ── перехват событий ───────────────────────

    def on_response(self, response) -> None:
        if response.status < 400:
            return
        url = response.url.split("?")[0][:200]
        entry = {"section": self.section, "status": response.status, "url": url}
        self.report.network_errors.append(entry)
        self.section_errors.append({"type": "network", **entry})
        self.log(f"  ⚠️ {response.status} {url[:100]}")

    def on_console(self, msg) -> None:
        if msg.type != "error":
            return
        # Дубли сетевых ошибок и favicon не считаем: их ловит on_response.
        if "Failed to load resource" in msg.text or "favicon" in msg.text:
            return
        text = msg.text[:400]
        entry = {"section": self.section, "text": text}
        self.report.console_errors.append(entry)
        self.section_errors.append({"type": "console_error", **entry})
        self.log(f"  ⚠️ JS: {text[:120]}")

    def on_pageerror(self, exc) -> None:
        text = str(exc)[:400]
        entry = {"section": self.section, "text": text}
        self.report.page_errors.append(entry)
        self.section_errors.append({"type": "pageerror", **entry})
        self.log(f"  ⚠️ pageerror: {text[:120]}")

    # ── логин ──────────────────────────────────

    def login(self) -> bool:
        cfg = self.config.get("login") or {}
        if not cfg.get("enabled"):
            return True
        self.section = "login"
        self.log("═══ LOGIN ═══")
        t0 = time.time()
        try:
            self.page.goto(self.abs_url(cfg.get("url", "/login")),
                           wait_until="domcontentloaded", timeout=self.timeout)
            for step in cfg.get("steps", []):
                if "fill" in step:
                    value = step.get("value", "")
                    if step.get("value_env"):
                        value = os.environ.get(step["value_env"], "")
                        if not value:
                            raise RuntimeError(f"пустая переменная окружения {step['value_env']}")
                    self.page.fill(step["fill"], str(value), timeout=self.timeout)
                elif "click" in step:
                    self.page.click(step["click"], timeout=self.timeout)
                elif "wait_url" in step:
                    self.page.wait_for_url(step["wait_url"], timeout=self.timeout)
                elif "wait_text" in step:
                    self.page.wait_for_selector(f"text={step['wait_text']}", timeout=self.timeout)
                elif "wait_ms" in step:
                    self.page.wait_for_timeout(int(step["wait_ms"]))
            self.wait_for_stable(min_idle_ms=1500, timeout_ms=15000)
            shot = self.screenshot("login_ok")
            self.add("login", "passed", shot=shot, ms=int((time.time() - t0) * 1000))
            self.log("  ✅ логин пройден")
            return True
        except Exception as exc:
            shot = self.screenshot("login_failed")
            self.add("login", "failed", error=str(exc)[:300], shot=shot,
                     ms=int((time.time() - t0) * 1000))
            self.log(f"  ❌ логин не удался: {exc}")
            return False

    # ── раздел ─────────────────────────────────

    def collect(self) -> dict:
        return self.page.evaluate(
            """() => ({
                inputs: [...document.querySelectorAll('input, textarea, select')].map(e => ({
                    tag: e.tagName.toLowerCase(), type: e.type || '',
                    name: e.name || '', placeholder: e.placeholder || '',
                    disabled: e.disabled, required: e.required,
                    hasLabel: !!(e.labels && e.labels.length) ||
                              !!(e.getAttribute('aria-label') || e.getAttribute('aria-labelledby'))
                })),
                buttons: [...document.querySelectorAll('button, [role=button], input[type=submit]')].map(e => ({
                    text: (e.innerText || e.value || '').trim().slice(0, 60),
                    disabled: e.disabled || e.getAttribute('aria-disabled') === 'true'
                })).filter(b => b.text),
                links: [...document.querySelectorAll('a[href]')].map(e => ({
                    text: (e.innerText || '').trim().slice(0, 60),
                    href: e.getAttribute('href')
                })).slice(0, 40),
                images: [...document.querySelectorAll('img')].map(e => ({
                    alt: e.getAttribute('alt'), src: e.getAttribute('src')
                })),
                h1: document.querySelectorAll('h1').length,
                title: document.title
            })"""
        )

    def test_links(self, data: dict) -> None:
        seen = set()
        for link in data.get("links", []):
            href = (link.get("href") or "").strip()
            if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")) or href in seen:
                continue
            seen.add(href)
            url = self.abs_url(href)
            t0 = time.time()
            try:
                resp = self.context.request.get(url, timeout=self.timeout,
                                                fail_on_status_code=False)
                status = resp.status
                if status >= 400:
                    self.add(f"link {href[:50]}", "warning",
                             error=f"HTTP {status} → {url[:120]}",
                             ms=int((time.time() - t0) * 1000))
                else:
                    self.add(f"link {href[:50]}", "passed", ms=int((time.time() - t0) * 1000))
            except Exception as exc:
                self.add(f"link {href[:50]}", "warning", error=str(exc)[:200],
                         ms=int((time.time() - t0) * 1000))

    def test_buttons(self, data: dict) -> None:
        skip = re.compile(self.config.get("skip_click_regex") or r"$^", re.I)
        current = self.page.url
        for btn in data.get("buttons", []):
            text = btn["text"]
            if btn["disabled"]:
                self.add(f"button {text[:40]}", "warning", error="disabled")
                continue
            if skip.search(text):
                self.add(f"button {text[:40]}", "passed", auto_fix="пропущено (опасное действие)")
                continue
            t0 = time.time()
            try:
                el = SmartSelector.find(self.page, text)
                if not el:
                    self.add(f"button {text[:40]}", "warning", error="элемент не найден")
                    continue
                el.click(timeout=5000)
                self.page.wait_for_timeout(400)
                shot = self.screenshot(f"click_{text[:20]}")
                self.add(f"button {text[:40]}", "passed", shot=shot,
                         ms=int((time.time() - t0) * 1000))
                if self.page.url != current:      # клик увёл со страницы — возвращаемся
                    self.page.goto(current, wait_until="domcontentloaded", timeout=self.timeout)
            except Exception as exc:
                self.add(f"button {text[:40]}", "warning", error=str(exc)[:200],
                         ms=int((time.time() - t0) * 1000))

    def test_inputs(self, data: dict) -> None:
        for inp in data.get("inputs", []):
            name = inp.get("name") or inp.get("placeholder") or inp["tag"]
            if inp["disabled"]:
                self.add(f"input {name}", "warning", error="disabled")
                continue
            if not inp["hasLabel"] and not inp.get("placeholder"):
                self.add(f"input {name}", "warning", error="нет label/aria-label/placeholder")
            else:
                self.add(f"input {name}", "passed")

    def deep_checks(self, data: dict) -> None:
        # a11y-минимум
        no_alt = [i for i in data.get("images", []) if i.get("src") and not i.get("alt")]
        if no_alt:
            self.add("a11y:img alt", "warning", error=f"{len(no_alt)} img без alt")
        else:
            self.add("a11y:img alt", "passed")
        if data.get("h1") == 1:
            self.add("a11y:h1", "passed")
        else:
            self.add("a11y:h1", "warning", error=f"h1 на странице: {data.get('h1')}")
        if not (data.get("title") or "").strip():
            self.add("a11y:title", "warning", error="пустой <title>")

        # адаптив: горизонтальный оверфлоу на 3 ширинах
        for width, height in ((1440, 900), (768, 1024), (390, 844)):
            try:
                self.page.set_viewport_size({"width": width, "height": height})
                self.page.wait_for_timeout(250)
                overflow = self.page.evaluate(
                    "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
                )
                label = f"responsive {width}px"
                if overflow > 2:
                    shot = self.screenshot(f"overflow_{width}")
                    self.add(label, "warning", error=f"горизонтальный оверфлоу {overflow}px", shot=shot)
                else:
                    self.add(label, "passed")
            except Exception as exc:
                self.add(f"responsive {width}px", "warning", error=str(exc)[:150])
        vp = self.config.get("viewport") or {"width": 1440, "height": 900}
        self.page.set_viewport_size(vp)

    # ── самолечение ────────────────────────────

    def try_autofix(self, err: dict) -> Optional[str]:
        """Диагностика 5xx через SSH. Правки кода не делает — только находит причину."""
        if err.get("type") != "network" or err.get("status", 0) < 500:
            return None
        if not self.ssh.enabled:
            return "SSH не настроен — нужна ручная диагностика"
        try:
            for service in self.config.get("services", {}):
                logs = self.ssh.get_logs(service, 40)
                if not logs:
                    continue
                cause = SSHDiagnostics.classify_error(logs)
                if cause == "Unknown":
                    continue
                endpoint = err["url"].split("//")[-1].split("/", 1)[-1]
                where = self.ssh.grep_code(endpoint.split("?")[0].split("/")[-1])
                if self.config["ssh"].get("allow_restart"):
                    self.ssh.restart_service(service)
                hint = where.splitlines()[0] if where else "совпадений в коде нет"
                return f"{cause} в {service} → {hint} (рестарт: {bool(self.config['ssh'].get('allow_restart'))})"
            return "в логах известных причин не найдено"
        except Exception as exc:
            return f"SSH-диагностика не удалась: {exc}"[:300]

    def process_errors(self) -> None:
        for err in self.section_errors:
            if err["type"] == "console_error":
                self.add("console error", "warning", error=err["text"][:200])
            elif err["type"] == "pageerror":
                self.add("pageerror", "failed", error=err["text"][:200])
            else:
                fix = self.try_autofix(err)
                if fix and "не " not in fix.split("→")[0][:12]:
                    self.add(f"HTTP {err['status']} {err['url'][:60]}", "auto_fixed",
                             error=err["url"][:120], auto_fix=fix)
                else:
                    self.add(f"HTTP {err['status']} {err['url'][:60]}", "failed",
                             error=err["url"][:120], auto_fix=fix)

    # ── прогон ─────────────────────────────────

    def run_section(self, sec: dict) -> None:
        self.section = sec["name"]
        self.section_errors = []
        url = self.abs_url(sec["url"])
        self.log(f"\n═══ {self.section.upper()} — {url} ═══")
        t0 = time.time()
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=self.timeout)
            try:
                self.page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            settled = self.wait_for_stable(min_idle_ms=2000, timeout_ms=sec.get("settle_ms", 20000))
            shot = self.screenshot(f"{self.section}_loaded")
            self.add("page_load", "passed", shot=shot, ms=int((time.time() - t0) * 1000))
            self.log(f"  ✅ загружено, стабильно через {settled} c, {self.page.url[:80]}")
        except Exception as exc:
            shot = self.screenshot(f"{self.section}_failed")
            self.add("page_load", "failed", error=str(exc)[:300], shot=shot,
                     ms=int((time.time() - t0) * 1000))
            self.log(f"  ❌ не загрузилось: {exc}")
            return

        try:
            data = self.collect()
        except Exception as exc:
            self.add("collect", "failed", error=str(exc)[:200])
            return
        self.report.total_elements += len(data["inputs"]) + len(data["buttons"])
        self.log(f"  🔍 элементов: {len(data['inputs'])} inputs, "
                 f"{len(data['buttons'])} buttons, {len(data['links'])} links")

        self.test_inputs(data)
        self.test_buttons(data)
        if self.deep:
            self.test_links(data)
            self.deep_checks(data)
        self.process_errors()

    def run(self) -> int:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.log("❌ Установите Playwright: pip install playwright && playwright install chromium")
            return 2

        cfg = self.config
        vp = cfg.get("viewport") or {"width": 1440, "height": 900}
        try:
            with sync_playwright() as pw:
                self.browser = pw.chromium.launch(
                    headless=self.headless,
                    slow_mo=int(cfg.get("slow_mo_ms", 0)),
                )
                self.context = self.browser.new_context(
                    viewport=vp,
                    http_credentials=cfg.get("http_credentials"),
                    ignore_https_errors=True,
                )
                self.page = self.context.new_page()
                self.page.set_default_timeout(self.timeout)
                self.page.on("response", self.on_response)
                self.page.on("console", self.on_console)
                self.page.on("pageerror", self.on_pageerror)

                if self.login():
                    for sec in cfg.get("sections", []):
                        self.run_section(sec)
                else:
                    self.add("routes", "warning", error="разделы пропущены: логин не удался")

                # Видимый браузер остаётся открытым — можно посмотреть глазами.
                if self.keep_open and not self.headless:
                    try:
                        input("\nБраузер открыт. Enter — закрыть... ")
                    except (EOFError, KeyboardInterrupt):
                        pass
        except Exception as exc:
            self.log(f"❌ Критическая ошибка: {exc}")
            traceback.print_exc()
            self.report.recount()
            self.save_report()
            return 2
        finally:
            if self.browser:
                try:
                    self.browser.close()
                except Exception:
                    pass

        self.report.duration_sec = int(time.time() - self.started)
        self.report.recount()
        self.save_report()
        self.print_summary()
        return 1 if self.report.failed else 0

    def save_report(self) -> None:
        self.report_path = os.path.join(self.shot_dir, "report.json")
        with open(self.report_path, "w", encoding="utf-8") as fh:
            json.dump(asdict(self.report), fh, ensure_ascii=False, indent=2)

    def print_summary(self) -> None:
        r = self.report
        bar = "=" * 62
        print(f"\n{bar}\n📋 ОТЧЁТ: {r.config_name}  ({r.base_url})\n"
              f"⏱ {r.duration_sec} c   📸 {os.path.abspath(self.shot_dir)}\n{bar}")
        print(f"  ✅ Passed:   {r.passed}\n  🔧 Auto-fix: {r.auto_fixed}\n"
              f"  ⚠️ Warnings: {r.warnings}\n  ❌ Failed:   {r.failed}")
        if r.auto_fixed:
            print("\n🔧 Диагностика/автофикс:")
            for res in r.results:
                if res.status in ("auto_fixed", "passed") and res.auto_fix and "пропущено" not in res.auto_fix:
                    print(f"  → {res.section}: {res.auto_fix}")
        if r.warnings and r.warnings <= 12:
            print("\n⚠️ Предупреждения:")
            for res in r.results:
                if res.status == "warning":
                    print(f"  ⚠️ {res.section} / {res.element}: {res.error}")
        if r.failed:
            print("\n❌ Требует ручного вмешательства:")
            for res in r.results:
                if res.status == "failed":
                    print(f"  ❌ {res.section} / {res.element}: {res.error}")
        print(f"\n📁 Полный отчёт: {os.path.abspath(self.report_path)}\n{bar}")


# ═══════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════

def load_config(path: Optional[str]) -> dict[str, Any]:
    if not path:
        return {}
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise SystemExit(f"Конфиг должен быть JSON-объектом: {path}")
    return data


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Имитест — автономный E2E-тестер (visible Playwright)")
    parser.add_argument("--config", help="JSON-файл конфигурации проекта")
    parser.add_argument("--url", help="Переопределить base_url")
    parser.add_argument("--deep", action="store_true", help="+ ссылки, адаптив, a11y-минимум")
    parser.add_argument("--headless", action="store_true", help="Без окна браузера (CI)")
    parser.add_argument("--shots", help="Каталог скриншотов и отчёта")
    parser.add_argument("--keep-open", action="store_true",
                        help="Оставить браузер открытым до нажатия Enter")
    parser.add_argument("--json", action="store_true", help="Печатать итог в JSON (для автоматизации)")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    if args.url:
        config["base_url"] = args.url
    if args.shots:
        config["screenshot_dir"] = args.shots

    tester = Imitester(config, deep=args.deep, headless=args.headless,
                       keep_open=args.keep_open)
    code = tester.run()
    if args.json:
        print(json.dumps({
            "exit_code": code,
            "report": os.path.abspath(getattr(tester, "report_path", "")),
            "passed": tester.report.passed,
            "failed": tester.report.failed,
            "warnings": tester.report.warnings,
            "auto_fixed": tester.report.auto_fixed,
        }, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
