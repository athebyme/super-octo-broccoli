#!/usr/bin/env python3
"""Generate the latest Wildberries report. Intended for cron/scheduled runs."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from app import (
    compute_profit_table,
    save_processed_report,
    ensure_directories,
    load_state,
    save_state,
)


def existing_file(value: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.exists():
        raise argparse.ArgumentTypeError(f"File not found: {path}")
    return path


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rebuild the latest Wildberries profit report.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--statistics",
        type=existing_file,
        help="Path to the Wildberries statistics XLSX file. Falls back to the last uploaded file stored in state.json.",
    )
    parser.add_argument(
        "--prices",
        type=existing_file,
        help="Path to the supplier price CSV file. Falls back to the last uploaded file stored in state.json.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)

    ensure_directories()
    state = load_state()

    statistics_path = args.statistics or state.get("statistics_path")
    price_path = args.prices or state.get("price_path")
    if not statistics_path or not Path(statistics_path).exists():
        print("No statistics file supplied and nothing found in state.json.", file=sys.stderr)
        return 1
    if not price_path or not Path(price_path).exists():
        print("No price catalog supplied and nothing found in state.json.", file=sys.stderr)
        return 1

    selected_columns = state.get("selected_columns")

    df, _, summary = compute_profit_table(Path(statistics_path), Path(price_path), selected_columns)
    output_path = save_processed_report(df, summary)

    state.update(
        {
            "statistics_path": str(statistics_path),
            "price_path": str(price_path),
            "selected_columns": selected_columns or [],
            "last_processed": datetime.now().isoformat(),
            "latest_output": str(output_path),
            "last_summary": summary,
        }
    )
    save_state(state)

    print(f"Report saved to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

