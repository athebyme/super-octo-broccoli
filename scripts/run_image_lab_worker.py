#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Durable Image Lab runner for production deployments."""

import argparse
import os
import time

os.environ.setdefault("SKIP_SCHEDULER", "1")
os.environ.setdefault("IMAGE_LAB_INLINE_WORKER", "0")

from seller_platform import app  # noqa: E402
from services.image_lab_service import process_pending_once  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--batch", type=int, default=4)
    args = parser.parse_args()
    interval = max(0.5, min(args.interval, 30.0))
    batch = max(1, min(args.batch, 20))
    print("Image Lab worker started", flush=True)
    while True:
        process_pending_once(app, limit=batch)
        # Remote jobs remain active for minutes; always throttle polling so an
        # unavailable bridge cannot turn into a hot loop.
        time.sleep(interval)


if __name__ == "__main__":
    main()
