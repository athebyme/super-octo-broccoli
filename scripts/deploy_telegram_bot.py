#!/usr/bin/env python3
"""One persistent long-poll receiver for deployment-bot subscriptions."""
import argparse
import json
from pathlib import Path
import signal
import sqlite3
import threading

try:
    from scripts.deploy_telegram import DEFAULT_CONFIG, BotError, Store, Telegram, configuration, poll_once
except ModuleNotFoundError:
    from deploy_telegram import DEFAULT_CONFIG, BotError, Store, Telegram, configuration, poll_once


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--once', action='store_true', help='One nonblocking update page; no deployment.')
    args = parser.parse_args()
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    try:
        config = configuration(args.config)
        store, telegram = Store(config), Telegram(config)
        with store.lock('receiver', nonblocking=True):
            username = telegram.preflight()
            print(json.dumps({'receiver':'ready','subscribers':len(store.recipients())}), flush=True)
            while not stop.is_set():
                try:
                    result = poll_once(store, telegram, username, timeout=0 if args.once else 20)
                    if result['commands'] or args.once:
                        print(json.dumps(result), flush=True)
                except BotError as error:
                    print(json.dumps({'receiver':'read_failed','code':error.code}), flush=True)
                    if error.code in (401, 409):
                        return 78
                    if args.once:
                        return 1
                    stop.wait(max(30, error.retry_after))
                if args.once:
                    return 0
    except BotError as error:
        print(json.dumps({'receiver':'stopped','code':error.code}), flush=True)
        return 78 if error.code in ('receiver_already_running','webhook_already_configured','invalid_bot_identity',401,409) else 1
    except (OSError, ValueError, sqlite3.Error):
        print(json.dumps({'receiver':'stopped','code':'local_state_or_configuration_error'}), flush=True)
        return 78
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
