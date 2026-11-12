"""Запустить unit- и API-тесты всех сервисов по очереди.

У каждого сервиса свои тесты в своей папке (сервисы независимы, общих тестов нет), поэтому
pytest запускается отдельно в каждой папке. Нужен установленный пакет pad_common:
    pip install -e libs/pad_common && pip install bcrypt pytest
Запуск из папки lab2:  python scripts/run_tests.py
e2e-тест (tests/e2e) запускается отдельно, когда система поднята в Docker (см. README).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIRS = ["libs/pad_common", "gateway", *sorted(str(p.relative_to(ROOT)) for p in (ROOT / "services").glob("*-service"))]


def main() -> int:
    failed = []
    for d in DIRS:
        print(f"\n=== {d} ===", flush=True)
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "tests"], cwd=ROOT / d)
        if r.returncode != 0:
            failed.append(d)
    print("\nFAILED: " + ", ".join(failed) if failed else "\nall service tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
