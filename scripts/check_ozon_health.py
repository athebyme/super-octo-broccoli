#!/usr/bin/env python3
"""Read-only local Ozon diagnostics. Does not import the Flask runtime/start jobs.

Exit 0: observed components have no attention signals; 1: attention; 2: no
complete observation. This never evaluates item publication permissions.
"""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path,default=Path('/app/data/seller_platform.db'))
    parser.add_argument('--seller-id',type=int,required=True)
    parser.add_argument('--account-id',type=int,required=True)
    args=parser.parse_args()
    engine=None
    try:
        from sqlalchemy import create_engine
        from services.ozon_account_health import observe_account
        uri=args.database.resolve().as_uri()+'?mode=ro'
        engine=create_engine('sqlite://',creator=lambda:sqlite3.connect(uri,uri=True,timeout=0.2))
        result=observe_account(seller_id=args.seller_id,account_id=args.account_id,
            config={'MARKETPLACE_OZON_ENABLED':os.environ.get('MARKETPLACE_OZON_ENABLED')=='1'},engine=engine)
        print(json.dumps(result,ensure_ascii=False,sort_keys=True))
        return 1 if result['needs_attention'] else 0
    except Exception as error:
        # Never print SQL, DB filenames, raw exceptions or secret-bearing config.
        print(json.dumps({'observation_only':True,'status':'unavailable',
                          'code':getattr(error,'code','local_health_unavailable')},ensure_ascii=False))
        return 2
    finally:
        if engine is not None:engine.dispose()


if __name__=='__main__':
    raise SystemExit(main())
