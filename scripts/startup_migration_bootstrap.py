#!/usr/bin/env python3
"""Perform the app/model bootstrap formerly embedded in docker-entrypoint."""
from __future__ import annotations

import argparse
import os
from pathlib import Path


def run(database_path: Path) -> None:
    os.environ["DATABASE_URL"] = f"sqlite:///{database_path.resolve()}"
    os.environ["SKIP_SCHEDULER"] = "1"

    from models import db
    from seller_platform import _run_startup_migrations, app, ensure_storage_roots

    ensure_storage_roots()
    with app.app_context():
        db.create_all()
        _run_startup_migrations()
        print("✅ Базовая структура БД создана")

        try:
            db.session.execute(db.text("PRAGMA journal_mode=WAL;"))
            db.session.execute(db.text("PRAGMA synchronous=NORMAL;"))
            db.session.execute(db.text("PRAGMA busy_timeout=30000;"))
            db.session.commit()
            print("✅ SQLite настроен: WAL mode включен, busy_timeout=30s")
        except Exception as exc:
            print(f"⚠️  Не удалось настроить SQLite WAL mode: {exc}")

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database_path", type=Path)
    args = parser.parse_args()
    run(args.database_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
