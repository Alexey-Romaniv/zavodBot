"""Применить миграции схемы к базе. Запуск: python migrate.py [--check]

Деплой вызывает это до перезапуска бота: если схема не поедет, лучше узнать
об этом здесь, а не в упавшем боте. `--check` ничего не меняет — только
печатает текущую версию и список неприменённых шагов.
"""
from __future__ import annotations

import logging
import sqlite3
import sys

import config
import migrations


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    check_only = "--check" in sys.argv[1:]

    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    try:
        print(f"База: {config.DB_PATH}")
        conn.executescript(migrations.HISTORY)
        print(migrations.describe(conn))
        if check_only:
            return 0
        applied = migrations.upgrade(conn, config.DB_PATH)
        if not applied:
            print("Нечего применять.")
        else:
            for m in applied:
                print(f"применено {m.version}: {m.name}")
            print(f"Схема на версии {migrations.TARGET}.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # деплой смотрит на код возврата
        logging.exception("Миграции не применились: %s", exc)
        raise SystemExit(1)
