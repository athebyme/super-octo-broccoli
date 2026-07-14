#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Admin CLI for curated agent knowledge ingestion, search and evaluation."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

os.environ.setdefault('SKIP_SCHEDULER', '1')
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from seller_platform import app  # noqa: E402
from models import db, AgentKnowledgeDocument  # noqa: E402
from services.agent_knowledge import (  # noqa: E402
    SOURCE_TYPES, evaluate_retrieval, ingest_document, search_knowledge,
)


def _print(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Управление курируемой базой знаний единого AI-помощника',
    )
    commands = parser.add_subparsers(dest='command', required=True)

    ingest = commands.add_parser('ingest', help='Добавить неизменяемую версию документа')
    ingest.add_argument('file', type=Path)
    ingest.add_argument('--title', required=True)
    ingest.add_argument('--source-key', required=True)
    ingest.add_argument('--source-type', required=True, choices=sorted(SOURCE_TYPES))
    ingest.add_argument('--source-uri', required=True)
    ingest.add_argument('--version', required=True)
    ingest.add_argument('--seller-id', type=int)
    ingest.add_argument('--language', default='ru')
    ingest.add_argument(
        '--valid-until',
        help='ISO-8601 freshness deadline; required for official sources',
    )
    ingest.add_argument('--metadata-json', default='{}')

    listing = commands.add_parser('list', help='Показать документы и их версии')
    listing.add_argument('--seller-id', type=int)
    listing.add_argument('--include-archived', action='store_true')

    archive = commands.add_parser('archive', help='Исключить версию из retrieval')
    archive.add_argument('document_id', type=int)

    search = commands.add_parser('search', help='Проверить retrieval без LLM')
    search.add_argument('query')
    search.add_argument('--seller-id', type=int, required=True)
    search.add_argument('--limit', type=int, default=6)

    evaluate = commands.add_parser('evaluate', help='Посчитать Recall@K и MRR')
    evaluate.add_argument('dataset', type=Path)
    evaluate.add_argument('--seller-id', type=int, required=True)
    evaluate.add_argument('--limit', type=int, default=6)
    return parser


def main() -> int:
    args = _parser().parse_args()
    with app.app_context():
        if args.command == 'ingest':
            if not args.file.is_file():
                raise SystemExit(f'Файл не найден: {args.file}')
            metadata = json.loads(args.metadata_json)
            result = ingest_document(
                title=args.title, source_key=args.source_key,
                source_type=args.source_type, source_uri=args.source_uri,
                version=args.version,
                content=args.file.read_text(encoding='utf-8'),
                seller_id=args.seller_id, language=args.language,
                metadata=metadata, valid_until=args.valid_until,
            )
            _print(result)
        elif args.command == 'list':
            query = AgentKnowledgeDocument.query
            if args.seller_id is not None:
                query = query.filter(AgentKnowledgeDocument.seller_id == args.seller_id)
            if not args.include_archived:
                query = query.filter_by(status='active')
            documents = query.order_by(
                AgentKnowledgeDocument.updated_at.desc(),
                AgentKnowledgeDocument.id.desc(),
            ).all()
            _print([{
                'id': item.id, 'seller_id': item.seller_id,
                'source_key': item.source_key, 'source_type': item.source_type,
                'source_uri': item.source_uri, 'title': item.title,
                'version': item.version, 'checksum': item.checksum,
                'valid_until': item.valid_until.isoformat() if item.valid_until else None,
                'status': item.status, 'chunks': item.chunks.count(),
                'updated_at': item.updated_at.isoformat() if item.updated_at else None,
            } for item in documents])
        elif args.command == 'archive':
            document = db.session.get(AgentKnowledgeDocument, args.document_id)
            if not document:
                raise SystemExit('Документ не найден')
            document.status = 'archived'
            db.session.commit()
            _print({'status': 'archived', 'document_id': document.id})
        elif args.command == 'search':
            _print(search_knowledge(
                seller_id=args.seller_id, query=args.query, limit=args.limit,
            ))
        elif args.command == 'evaluate':
            cases = json.loads(args.dataset.read_text(encoding='utf-8'))
            _print(evaluate_retrieval(
                cases, seller_id=args.seller_id, limit=args.limit,
            ))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
