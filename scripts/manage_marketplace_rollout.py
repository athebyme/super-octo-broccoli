#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Secret-free operational CLI for P11 backfill, parity and recovery."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


os.environ.setdefault("SKIP_SCHEDULER", "1")
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from seller_platform import app  # noqa: E402
from services.marketplace_readiness import MarketplaceReadinessService  # noqa: E402
from services.marketplace_rollout import MarketplaceRolloutService  # noqa: E402


def _print(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Bounded WB projection/parity maintenance. Команды не вызывают "
            "WB, Ozon или LLM и не печатают credentials."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    status = commands.add_parser(
        "status",
        help="Показать seller-scoped readiness и безопасные агрегаты",
    )
    status.add_argument("--seller-id", type=int, required=True)

    tick = commands.add_parser(
        "tick",
        help="Один scheduler-equivalent bounded maintenance tick",
    )
    tick.add_argument("--seller-limit", type=int, default=3)
    tick.add_argument("--batch-size", type=int, default=200)
    tick.add_argument("--without-parity", action="store_true")

    backfill = commands.add_parser(
        "backfill",
        help="Обработать максимум один WB backfill batch",
    )
    backfill.add_argument("--seller-id", type=int, required=True)
    backfill.add_argument("--batch-size", type=int, default=200)
    backfill.add_argument(
        "--force-full",
        action="store_true",
        help="Начать новый full repair sweep; этот вызов всё равно bounded",
    )

    parity = commands.add_parser(
        "parity",
        help="Обработать максимум один dual-read comparison batch",
    )
    parity.add_argument("--seller-id", type=int, required=True)
    parity.add_argument("--batch-size", type=int, default=200)
    parity.add_argument("--force-full", action="store_true")

    pause = commands.add_parser(
        "pause",
        help="Поставить неисполняющийся projection run на паузу",
    )
    pause.add_argument("--run-id", type=int, required=True)

    resume = commands.add_parser(
        "resume",
        help="Возобновить paused/failed run с сохранённого cursor",
    )
    resume.add_argument("--run-id", type=int, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    with app.app_context():
        if args.command == "status":
            _print(MarketplaceReadinessService.build(
                seller_id=args.seller_id,
                config=app.config,
            ))
        elif args.command == "tick":
            _print(MarketplaceRolloutService.maintenance_tick(
                seller_limit=args.seller_limit,
                batch_size=args.batch_size,
                dual_read_enabled=not args.without_parity,
            ))
        elif args.command == "backfill":
            run = MarketplaceRolloutService.run_backfill_batch(
                seller_id=args.seller_id,
                limit=args.batch_size,
                force_full=args.force_full,
            )
            _print({"run": run.to_public_dict() if run else None})
        elif args.command == "parity":
            run = MarketplaceRolloutService.run_parity_batch(
                seller_id=args.seller_id,
                limit=args.batch_size,
                force_full=args.force_full,
            )
            _print({"run": run.to_public_dict() if run else None})
        elif args.command == "pause":
            _print(MarketplaceRolloutService.pause_run(
                run_id=args.run_id,
            ).to_public_dict())
        elif args.command == "resume":
            _print(MarketplaceRolloutService.resume_run(
                run_id=args.run_id,
            ).to_public_dict())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
