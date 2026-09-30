#!/usr/bin/env python3
"""Broadcast one authorized milestone to the deployment bot's active subscribers."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

try:
    from scripts.deploy_telegram import DEFAULT_CONFIG, KEYS, BotError, Store, Telegram, broadcast, configuration
except ModuleNotFoundError:  # Direct `python scripts/notify_task_status.py`.
    from deploy_telegram import DEFAULT_CONFIG, KEYS, BotError, Store, Telegram, broadcast, configuration


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--message-file', required=True, type=Path)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--parse-mode', choices=['HTML'])
    args = parser.parse_args()
    try:
        message = args.message_file.read_text().strip()
        if not message or len(message.encode('utf-16-le')) // 2 > 3500:
            raise ValueError('invalid message length')
        config = configuration(args.config)
        store = Store(config)
        result = broadcast(store, Telegram(config), message, parse_mode=args.parse_mode)
    except (OSError, UnicodeError, ValueError, sqlite3.Error, BotError):
        print('Telegram broadcast unconfirmed: check local configuration/state; do not repeat blindly.', file=sys.stderr)
        return 2
    print(json.dumps(result))
    return 0 if result['sent'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
