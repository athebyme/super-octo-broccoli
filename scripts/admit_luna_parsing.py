#!/usr/bin/env python3
"""Seal or admit a reviewed parsing chunk without bootstrapping the application."""

import argparse
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flask import Flask
from models import db
from scripts.validate_luna_parsing import ContractError, read_json, write_report
from services.supplier_luna_enrichment import prepare_batch, apply_batch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['seal', 'apply'])
    parser.add_argument('--database', required=True, type=Path)
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--admin-id', type=int)
    parser.add_argument('--reviewed-ids', help='Exact root-reviewed IDs, comma separated')
    parser.add_argument('--approve-inference', action='append', default=[],
                        metavar='PRODUCT_ID:FIELD_ID',
                        help='Explicitly admit one reviewed inference; may be repeated')
    parser.add_argument('--reject-field', action='append', default=[],
                        metavar='PRODUCT_ID:FIELD_ID:REASON_CODE',
                        help='Withdraw one previously admitted field with an audited reason')
    args = parser.parse_args()
    if args.report.resolve() in {args.input.resolve(),
                                 args.output.resolve() if args.output else None,
                                 args.database.resolve()}:
        parser.error('report must not replace an input or the database')
    if args.command == 'apply' and not (args.output and args.admin_id and args.reviewed_ids):
        parser.error('apply requires --output, --admin-id and --reviewed-ids')
    mode = 'ro' if args.command == 'seal' else 'rw'

    def connection():
        conn = sqlite3.connect(args.database.resolve().as_uri() + f'?mode={mode}',
                               uri=True, timeout=2)
        conn.execute('PRAGMA foreign_keys=ON')
        if mode == 'ro':
            conn.execute('PRAGMA query_only=ON')
        return conn

    app = Flask('luna-admission')
    app.config.update(SQLALCHEMY_DATABASE_URI='sqlite://',
                      SQLALCHEMY_TRACK_MODIFICATIONS=False,
                      SQLALCHEMY_ENGINE_OPTIONS={'creator': connection})
    db.init_app(app)
    with app.app_context():
        try:
            batch, _ = read_json(args.input)
            if args.command == 'seal':
                report = prepare_batch(batch)
            else:
                output, _ = read_json(args.output)
                reviewed = [int(value) for value in args.reviewed_ids.split(',')]
                approved = {}
                for identity in args.approve_inference:
                    product_id, field_id = map(int, identity.split(':'))
                    approved.setdefault(product_id, []).append(field_id)
                rejected = {}
                for identity in args.reject_field:
                    product_id, field_id, reason = identity.split(':')
                    fields = rejected.setdefault(int(product_id), {})
                    if int(field_id) in fields:
                        parser.error('duplicate field rejection')
                    fields[int(field_id)] = reason
                report = apply_batch(batch, output, admin_user_id=args.admin_id,
                                     reviewed_product_ids=reviewed,
                                     approved_inferences=approved, field_rejections=rejected)
            write_report(args.report, report)
            print(json.dumps({'sealed': True, 'items': len(report['items'])}
                             if args.command == 'seal' else report))
        except ContractError as exc:
            db.session.rollback()
            report = {'applied': 0, 'error_code': str(exc)}
            write_report(args.report, report)
            print(json.dumps(report))
            return 1
        finally:
            db.session.rollback()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
