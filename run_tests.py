#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Прогнать все проверки разом.

    python3 run_tests.py

Каждый набор запускается отдельным процессом на своей временной базе:
тесты не видят чужих данных и не зависят от порядка. Возвращает 1, если
упал хоть один — годится для шага перед выкаткой.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent
SITE = ROOT.parent / "tihiy-chas-web"

SUITES = [
    ("test_smoke.py", "данные и бизнес-логика"),
    ("test_scenario.py", "путь гостя через хендлеры бота"),
    ("test_cabinet_stage2.py", "онбординг заведения и модерация"),
    ("test_migrations.py", "миграции схемы"),
    ("test_redeem.py", "погашение кода на входе"),
    ("test_slot_race.py", "гонка за последний слот"),
    ("test_degraded.py", "код не проходит — гостя впускаем"),
    ("test_notify.py", "очередь уведомлений и экран смены"),
    ("test_timezone.py", "одно «сегодня» на весь продукт"),
]


def run_suite(name: str) -> tuple[bool, str, float]:
    db = pathlib.Path(tempfile.mkdtemp()) / "t.db"
    env = {**__import__("os").environ,
           "DB_URL": f"sqlite+aiosqlite:///{db}",
           "PYTHONPATH": str(ROOT)}
    t0 = time.monotonic()
    p = subprocess.run([sys.executable, str(ROOT / name)],
                       capture_output=True, text=True, env=env)
    return p.returncode == 0, (p.stdout + p.stderr), time.monotonic() - t0


def main() -> int:
    print("Проверки «Тихого Часа»\n" + "─" * 52)
    failed: list[tuple[str, str]] = []

    for name, what in SUITES:
        if not (ROOT / name).exists():
            print(f"  ⚠ {name} — файла нет, пропускаю")
            continue
        ok, out, secs = run_suite(name)
        mark = "✓" if ok else "✗"
        print(f"  {mark} {name:<26} {what:<34} {secs:4.1f}с")
        if not ok:
            failed.append((name, out))

    # Продуктовые константы: цифра, поправленная прямо в разметке, здесь
    # и ловится — иначе сайт снова разъедется сам с собой.
    if SITE.is_dir():
        print("─" * 52)
        for tool, what in (("tools/product.py", "константы сходятся с разметкой"),
                           ("tools/venues_snapshot.py", "витрина сходится с данными")):
            p = subprocess.run([sys.executable, str(SITE / tool), "--check"],
                               capture_output=True, text=True, cwd=SITE)
            ok = p.returncode == 0
            print(f"  {'✓' if ok else '✗'} {tool:<26} {what}")
            if not ok:
                failed.append((tool, p.stdout + p.stderr))
    else:
        print(f"  ⚠ репозиторий сайта не найден ({SITE}) — проверки контента пропущены")

    print("─" * 52)
    if failed:
        print(f"УПАЛО: {len(failed)}\n")
        for name, out in failed:
            print(f"───── {name} " + "─" * (46 - len(name)))
            print("\n".join(out.strip().splitlines()[-25:]))
            print()
        return 1
    print("Всё прошло.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
