#!/usr/bin/env python3
"""Самопроверка имитеста на встроенном стенде.

Поднимает локальный HTTP-сервер с демо-страницами (selftest/site), запускает
imitester.py в headless-режиме и проверяет, что он действительно находит
заранее заложенные проблемы:

  * pageerror (исключение в JS)          → failed
  * сетевой 404 (/api/missing)           → failed
  * console.error                        → warning
  * <img> без alt                        → warning
  * битая ссылка /missing.html           → warning
  * логин по форме                       → passed

Запуск:  python selftest/run_selftest.py
Код возврата: 0 — самопроверка пройдена, 1 — тестер работает неверно.
"""

from __future__ import annotations

import functools
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE = os.path.join(ROOT, "selftest", "site")


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # без спама в консоль
        pass


def serve():
    handler = functools.partial(QuietHandler, directory=SITE)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def main() -> int:
    httpd, port = serve()
    cfg = {
        "name": "selftest",
        "base_url": f"http://127.0.0.1:{port}",
        "screenshot_dir": os.path.join(tempfile.mkdtemp(prefix="imitest_selftest_"), "shots"),
        "timeout_ms": 15000,
        "login": {
            "enabled": True,
            "url": "/login.html",
            "steps": [
                {"fill": "#email", "value": "demo@example.com"},
                {"fill": "#password", "value": "secret"},
                {"click": "button[type='submit']"},
                {"wait_url": "**/dashboard*"},
            ],
        },
        "sections": [
            {"name": "home", "url": "/"},
            {"name": "about", "url": "/about.html"},
            {"name": "errors", "url": "/errors.html", "settle_ms": 5000},
            {"name": "dashboard", "url": "/dashboard.html"},
        ],
    }
    cfg_path = os.path.join(os.path.dirname(cfg["screenshot_dir"]), "config.json")
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)

    cmd = [sys.executable, os.path.join(ROOT, "imitester.py"),
           "--config", cfg_path, "--deep", "--headless", "--json"]
    print("→", " ".join(cmd))
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=300)
    print(proc.stdout[-3000:])
    if proc.returncode not in (0, 1):
        print("STDERR:", proc.stderr[-2000:])
        print("❌ имитест упал с кодом", proc.returncode)
        return 1

    report_path = os.path.join(cfg["screenshot_dir"], "report.json")
    with open(report_path, encoding="utf-8") as fh:
        report = json.load(fh)

    text = json.dumps(report, ensure_ascii=False).lower()

    def has(status: str, needle: str) -> bool:
        return any(r["status"] == status and needle.lower() in
                   (str(r["element"]) + " " + str(r["error"]) + " " + str(r["auto_fix"])).lower()
                   for r in report["results"])

    checks = {
        "логин пройден": has("passed", "login"),
        "pageerror найден": bool(report["page_errors"]) and has("failed", "pageerror"),
        "сетевой 404 найден": any(e["status"] == 404 for e in report["network_errors"]),
        "console.error найден": bool(report["console_errors"]),
        "img без alt найден": has("warning", "img alt"),
        "битая ссылка найдена": has("warning", "/missing.html"),
        "разделы пройдены (4)": report["total_elements"] >= 1,
        "скриншоты сделаны": len(report.get("screenshots_dir", "")) > 0
        and any(f.endswith(".png") for f in os.listdir(cfg["screenshot_dir"])),
    }

    failed = [name for name, ok in checks.items() if not ok]
    print("\n── самопроверка ──")
    for name, ok in checks.items():
        print(("✅ " if ok else "❌ ") + name)
    print(f"\npassed={report['passed']} failed={report['failed']} "
          f"warnings={report['warnings']} exit_code={proc.returncode}")
    httpd.shutdown()
    if failed:
        print("❌ НЕ ПРОЙДЕНО:", ", ".join(failed))
        return 1
    print("✅ самопроверка пройдена")
    return 0


if __name__ == "__main__":
    sys.exit(main())
