# -*- coding: utf-8 -*-
"""Curated, tenant-aware hybrid retrieval for the unified seller agent.

Only explicitly ingested unstructured guidance belongs here. Product data,
prices, stock, WB dictionaries and live statuses stay in their typed SQL paths.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import weakref
from collections import defaultdict
from datetime import datetime, timezone
from urllib.parse import urlsplit

from sqlalchemy import or_, text

from models import db, AgentKnowledgeChunk, AgentKnowledgeDocument, Seller


MAX_DOCUMENT_CHARS = 500_000
MAX_QUERY_CHARS = 500
MAX_RETRIEVAL_HITS = 8
MAX_CONTEXT_CHARS = 6_000
DEFAULT_CHUNK_CHARS = 1_400

SOURCE_TYPES = frozenset({
    'wb_official', 'seller_policy', 'platform_guide', 'official_reference',
})

_STOP_WORDS = frozenset({
    'а', 'без', 'бы', 'в', 'во', 'для', 'до', 'за', 'и', 'из', 'или', 'как',
    'к', 'ко', 'ли', 'на', 'не', 'но', 'о', 'об', 'от', 'по', 'под', 'при',
    'про', 'с', 'со', 'у', 'что', 'это', 'the', 'a', 'an', 'and', 'or', 'of',
    'for', 'in', 'on', 'to', 'with',
})

_SECRET_PATTERNS = (
    re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----'),
    re.compile(
        r'(?im)^\s*(?:api[_-]?key|secret(?:_key)?|password|access[_-]?token)'
        r'\s*[:=]\s*[^\s]{12,}\s*$',
    ),
)

_FTS_READY_ENGINES = weakref.WeakKeyDictionary()


def _normalized(value: str) -> str:
    return re.sub(r'\s+', ' ', str(value or '').casefold()).strip()


def _query_tokens(value: str) -> list[str]:
    raw = re.findall(r'[0-9a-zа-яё]{2,}', _normalized(value))
    filtered = [token for token in raw if token not in _STOP_WORDS]
    return (filtered or raw)[:20]


def _prefix(token: str) -> str:
    # Five Unicode characters preserve useful Russian stems ("описан*") while
    # FTS/reranking removes the broad false positives afterwards.
    return token if len(token) <= 5 else token[:5]


def _scope_key(seller_id: int | None) -> str:
    return 'global' if seller_id is None else f'seller:{seller_id}'


def _validate_source(source_type: str, source_uri: str, seller_id: int | None) -> None:
    if source_type not in SOURCE_TYPES:
        raise ValueError(f'source_type must be one of: {", ".join(sorted(SOURCE_TYPES))}')
    parsed = urlsplit(source_uri)
    host = (parsed.hostname or '').casefold()
    if source_type == 'wb_official':
        if parsed.scheme != 'https' or not (
            host == 'wildberries.ru' or host.endswith('.wildberries.ru')
        ):
            raise ValueError('wb_official source_uri must be an HTTPS Wildberries URL')
    elif source_type == 'seller_policy':
        if seller_id is None:
            raise ValueError('seller_policy documents require seller_id')
        if parsed.scheme not in {'seller', 'https'}:
            raise ValueError('seller_policy source_uri must use seller:// or https://')
    elif source_type == 'platform_guide':
        if parsed.scheme != 'sellerhub':
            raise ValueError('platform_guide source_uri must use sellerhub://')
    elif parsed.scheme != 'https':
        raise ValueError('official_reference source_uri must use HTTPS')


def _validate_document(
    *, title: str, source_key: str, source_type: str, source_uri: str,
    version: str, content: str, seller_id: int | None, metadata: dict | None,
    valid_until,
) -> tuple[str, str, datetime | None]:
    title = re.sub(r'\s+', ' ', str(title or '')).strip()
    source_key = str(source_key or '').strip()
    version = str(version or '').strip()
    content = str(content or '').replace('\x00', '').replace('\r\n', '\n').strip()
    if not 3 <= len(title) <= 300:
        raise ValueError('title must contain 3..300 characters')
    if not re.fullmatch(r'[\w./:-]{2,160}', source_key, flags=re.UNICODE):
        raise ValueError('source_key must be a stable 2..160 character identifier')
    if not re.fullmatch(r'[\w.:-]{1,80}', version, flags=re.UNICODE):
        raise ValueError('version must contain only letters, digits, dot, colon, dash or underscore')
    if not 20 <= len(content) <= MAX_DOCUMENT_CHARS:
        raise ValueError(f'content must contain 20..{MAX_DOCUMENT_CHARS} characters')
    if not isinstance(metadata or {}, dict):
        raise ValueError('metadata must be an object')
    if len(json.dumps(metadata or {}, ensure_ascii=False)) > 10_000:
        raise ValueError('metadata is too large')
    if any(pattern.search(content) for pattern in _SECRET_PATTERNS):
        raise ValueError('document looks like it contains a credential or private key')
    if seller_id is not None and db.session.get(Seller, seller_id) is None:
        raise ValueError('seller_id does not exist')
    _validate_source(source_type, source_uri, seller_id)
    if valid_until in (None, ''):
        parsed_valid_until = None
    elif isinstance(valid_until, datetime):
        parsed_valid_until = valid_until
    elif isinstance(valid_until, str):
        try:
            parsed_valid_until = datetime.fromisoformat(valid_until.replace('Z', '+00:00'))
        except ValueError as exc:
            raise ValueError('valid_until must be an ISO-8601 datetime') from exc
    else:
        raise ValueError('valid_until must be an ISO-8601 datetime')
    if parsed_valid_until and parsed_valid_until.tzinfo is not None:
        parsed_valid_until = parsed_valid_until.astimezone(timezone.utc).replace(tzinfo=None)
    if source_type in {'wb_official', 'official_reference'} and not parsed_valid_until:
        raise ValueError('official documents require valid_until for fail-closed freshness')
    if parsed_valid_until and parsed_valid_until <= datetime.utcnow():
        raise ValueError('valid_until must be in the future')
    return title, content, parsed_valid_until


def _split_long_paragraph(value: str, limit: int) -> list[str]:
    if len(value) <= limit:
        return [value]
    sentences = re.split(r'(?<=[.!?;:])\s+', value)
    parts: list[str] = []
    current = ''
    for sentence in sentences:
        if len(sentence) > limit:
            if current:
                parts.append(current)
                current = ''
            for start in range(0, len(sentence), limit):
                parts.append(sentence[start:start + limit].strip())
            continue
        candidate = f'{current} {sentence}'.strip()
        if current and len(candidate) > limit:
            parts.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        parts.append(current)
    return [part for part in parts if part]


def chunk_document(content: str, max_chars: int = DEFAULT_CHUNK_CHARS) -> list[dict]:
    """Split Markdown/plain text along headings and paragraphs."""
    max_chars = min(max(int(max_chars), 600), 2_000)
    units: list[tuple[str, str]] = []
    heading = ''
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            value = re.sub(r'\s+', ' ', ' '.join(paragraph)).strip()
            if value:
                units.append((heading, value))
            paragraph.clear()

    for raw_line in content.splitlines():
        line = raw_line.strip()
        match = re.match(r'^#{1,6}\s+(.+?)\s*#*$', line)
        if match:
            flush()
            heading = re.sub(r'\s+', ' ', match.group(1)).strip()[:300]
        elif not line:
            flush()
        else:
            paragraph.append(line)
    flush()
    if not units:
        units = [('', re.sub(r'\s+', ' ', content).strip())]

    chunks: list[dict] = []
    current_heading = ''
    current_parts: list[str] = []
    current_length = 0

    def emit() -> None:
        nonlocal current_parts, current_length
        if not current_parts:
            return
        value = '\n\n'.join(current_parts).strip()
        chunks.append({
            'ordinal': len(chunks),
            'heading': current_heading or None,
            'content': value,
        })
        current_parts = []
        current_length = 0

    for unit_heading, paragraph_text in units:
        for part in _split_long_paragraph(paragraph_text, max_chars):
            added = len(part) + (2 if current_parts else 0)
            if current_parts and (
                unit_heading != current_heading or current_length + added > max_chars
            ):
                emit()
            if not current_parts:
                current_heading = unit_heading
            current_parts.append(part)
            current_length += added
    emit()
    return chunks


def ensure_fts_schema() -> bool:
    """Create/reconcile FTS5 for create_all-based test and local databases."""
    bind = db.session.get_bind()
    if bind.dialect.name != 'sqlite':
        return False
    if _FTS_READY_ENGINES.get(bind):
        return True
    exists = db.session.execute(text(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'agent_knowledge_chunks_fts' LIMIT 1",
    )).first()
    if exists:
        # Production gets the full schema and backfill from the fail-fast
        # migration. Avoid DDL/count scans on every read-only retrieval.
        _FTS_READY_ENGINES[bind] = True
        return True
    statements = (
        """CREATE VIRTUAL TABLE IF NOT EXISTS agent_knowledge_chunks_fts USING fts5(
               chunk_id UNINDEXED, title, heading, content,
               tokenize='unicode61 remove_diacritics 2'
           )""",
        """CREATE TRIGGER IF NOT EXISTS agent_knowledge_chunks_fts_ai
           AFTER INSERT ON agent_knowledge_chunks BEGIN
             INSERT INTO agent_knowledge_chunks_fts(rowid, chunk_id, title, heading, content)
             VALUES (
               new.id, new.id,
               COALESCE((SELECT title FROM agent_knowledge_documents WHERE id = new.document_id), ''),
               COALESCE(new.heading, ''), new.content
             );
           END""",
        """CREATE TRIGGER IF NOT EXISTS agent_knowledge_chunks_fts_ad
           AFTER DELETE ON agent_knowledge_chunks BEGIN
             DELETE FROM agent_knowledge_chunks_fts WHERE rowid = old.id;
           END""",
        """CREATE TRIGGER IF NOT EXISTS agent_knowledge_chunks_fts_au
           AFTER UPDATE OF heading, content ON agent_knowledge_chunks BEGIN
             DELETE FROM agent_knowledge_chunks_fts WHERE rowid = old.id;
             INSERT INTO agent_knowledge_chunks_fts(rowid, chunk_id, title, heading, content)
             VALUES (
               new.id, new.id,
               COALESCE((SELECT title FROM agent_knowledge_documents WHERE id = new.document_id), ''),
               COALESCE(new.heading, ''), new.content
             );
           END""",
    )
    for statement in statements:
        db.session.execute(text(statement))
    counts = db.session.execute(text(
        """SELECT
             (SELECT COUNT(*) FROM agent_knowledge_chunks) AS chunks_count,
             (SELECT COUNT(*) FROM agent_knowledge_chunks_fts) AS fts_count""",
    )).mappings().one()
    if int(counts['chunks_count']) != int(counts['fts_count']):
        db.session.execute(text('DELETE FROM agent_knowledge_chunks_fts'))
        db.session.execute(text(
            """INSERT INTO agent_knowledge_chunks_fts(rowid, chunk_id, title, heading, content)
               SELECT c.id, c.id, d.title, COALESCE(c.heading, ''), c.content
               FROM agent_knowledge_chunks c
               JOIN agent_knowledge_documents d ON d.id = c.document_id""",
        ))
    _FTS_READY_ENGINES[bind] = True
    return True


def ingest_document(
    *, title: str, source_key: str, source_type: str, source_uri: str,
    version: str, content: str, seller_id: int | None = None,
    language: str = 'ru', metadata: dict | None = None, valid_until=None,
) -> dict:
    """Atomically add one immutable version and archive older source versions."""
    title, content, parsed_valid_until = _validate_document(
        title=title, source_key=source_key, source_type=source_type,
        source_uri=source_uri, version=version, content=content,
        seller_id=seller_id, metadata=metadata, valid_until=valid_until,
    )
    language = str(language or 'ru').strip().lower()
    if not re.fullmatch(r'[a-z]{2}(?:-[a-z]{2})?', language):
        raise ValueError('language must look like ru or ru-ru')
    checksum = hashlib.sha256(content.encode('utf-8')).hexdigest()
    scope_key = _scope_key(seller_id)
    existing = AgentKnowledgeDocument.query.filter_by(
        scope_key=scope_key, source_key=source_key, version=version,
    ).first()
    if existing:
        if existing.checksum != checksum:
            raise ValueError('this source version already exists with another checksum')
        return {
            'status': 'unchanged', 'document_id': existing.id,
            'checksum': checksum, 'chunks': existing.chunks.count(),
        }

    chunks = chunk_document(content)
    if not chunks:
        raise ValueError('document produced no chunks')
    try:
        ensure_fts_schema()
        AgentKnowledgeDocument.query.filter_by(
            scope_key=scope_key, source_key=source_key, status='active',
        ).update({'status': 'archived', 'updated_at': datetime.utcnow()})
        document = AgentKnowledgeDocument(
            seller_id=seller_id, scope_key=scope_key, source_key=source_key,
            source_type=source_type, source_uri=source_uri, title=title,
            version=version, checksum=checksum, language=language,
            status='active', valid_until=parsed_valid_until,
            metadata_json=json.dumps(metadata or {}, ensure_ascii=False, separators=(',', ':')),
        )
        db.session.add(document)
        db.session.flush()
        for item in chunks:
            searchable = _normalized(
                f'{title} {item.get("heading") or ""} {item["content"]}',
            )
            db.session.add(AgentKnowledgeChunk(
                document_id=document.id,
                ordinal=item['ordinal'],
                heading=item.get('heading'), content=item['content'],
                search_text=searchable,
                token_estimate=max(1, math.ceil(len(item['content'].encode('utf-8')) / 2)),
            ))
        db.session.commit()
        return {
            'status': 'created', 'document_id': document.id,
            'checksum': checksum, 'chunks': len(chunks),
        }
    except Exception:
        db.session.rollback()
        raise


def _trigrams(value: str) -> set[str]:
    compact = re.sub(r'\s+', ' ', _normalized(value))
    if len(compact) < 3:
        return {compact} if compact else set()
    return {compact[index:index + 3] for index in range(len(compact) - 2)}


def _trigram_containment(query: str, candidate: str) -> float:
    query_parts = _trigrams(query)
    if not query_parts:
        return 0.0
    return len(query_parts & _trigrams(candidate)) / len(query_parts)


def _snippet(content: str, query: str, prefixes: list[str], limit: int = 1_050) -> str:
    if len(content) <= limit:
        return content.strip()
    lowered = content.casefold()
    position = lowered.find(query.casefold())
    if position < 0:
        positions = [lowered.find(item) for item in prefixes if lowered.find(item) >= 0]
        position = min(positions) if positions else 0
    start = max(0, position - limit // 3)
    end = min(len(content), start + limit)
    start = max(0, end - limit)
    value = content[start:end].strip()
    return f'{"…" if start else ""}{value}{"…" if end < len(content) else ""}'


def search_knowledge(
    *, seller_id: int, query: str, limit: int = 6,
    max_chars: int = MAX_CONTEXT_CHARS,
) -> dict:
    """Retrieve scoped chunks using FTS prefix rank + deterministic reranking."""
    if not isinstance(seller_id, int) or isinstance(seller_id, bool) or seller_id <= 0:
        raise ValueError('seller_id must be a positive integer')
    query = re.sub(r'\s+', ' ', str(query or '')).strip()
    if not 2 <= len(query) <= MAX_QUERY_CHARS:
        raise ValueError(f'query must contain 2..{MAX_QUERY_CHARS} characters')
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise ValueError('limit must be an integer')
    if not isinstance(max_chars, int) or isinstance(max_chars, bool):
        raise ValueError('max_chars must be an integer')
    limit = min(max(limit, 1), MAX_RETRIEVAL_HITS)
    max_chars = min(max(max_chars, 500), MAX_CONTEXT_CHARS)
    tokens = _query_tokens(query)
    prefixes = list(dict.fromkeys(_prefix(token) for token in tokens))
    normalized_query = _normalized(query)

    visible = (
        AgentKnowledgeDocument.status == 'active',
        or_(
            AgentKnowledgeDocument.valid_until.is_(None),
            AgentKnowledgeDocument.valid_until > datetime.utcnow(),
        ),
        or_(
            AgentKnowledgeDocument.seller_id.is_(None),
            AgentKnowledgeDocument.seller_id == seller_id,
        ),
    )
    fts_positions: dict[int, int] = {}
    fts_used = False
    if prefixes and ensure_fts_schema():
        match_query = ' OR '.join(f'{item}*' for item in prefixes)
        rows = db.session.execute(text(
            """SELECT c.id AS chunk_id,
                      bm25(agent_knowledge_chunks_fts, 0.0, 3.0, 2.0, 1.0) AS rank
               FROM agent_knowledge_chunks_fts
               JOIN agent_knowledge_chunks c
                 ON c.id = agent_knowledge_chunks_fts.rowid
               JOIN agent_knowledge_documents d ON d.id = c.document_id
               WHERE agent_knowledge_chunks_fts MATCH :match_query
                 AND d.status = 'active'
                 AND (d.valid_until IS NULL OR d.valid_until > :now)
                 AND (d.seller_id IS NULL OR d.seller_id = :seller_id)
               ORDER BY rank ASC
               LIMIT 160""",
        ), {
            'match_query': match_query, 'seller_id': seller_id,
            'now': datetime.utcnow(),
        }).mappings().all()
        fts_positions = {int(row['chunk_id']): index for index, row in enumerate(rows)}
        fts_used = True

    fallback_query = AgentKnowledgeChunk.query.join(AgentKnowledgeDocument).filter(*visible)
    if prefixes:
        fallback_query = fallback_query.filter(or_(
            *(AgentKnowledgeChunk.search_text.like(f'%{item}%') for item in prefixes)
        ))
    fallback_ids = [row.id for row in fallback_query.order_by(
        AgentKnowledgeDocument.updated_at.desc(), AgentKnowledgeChunk.id.desc(),
    ).limit(240).all()]
    candidate_ids = list(dict.fromkeys([*fts_positions, *fallback_ids]))
    if not candidate_ids:
        # Bounded typo fallback: never scan the entire corpus in Python.
        candidate_ids = [row.id for row in AgentKnowledgeChunk.query.join(
            AgentKnowledgeDocument,
        ).filter(*visible).order_by(
            AgentKnowledgeDocument.updated_at.desc(), AgentKnowledgeChunk.id.desc(),
        ).limit(240).all()]
    candidates = AgentKnowledgeChunk.query.filter(
        AgentKnowledgeChunk.id.in_(candidate_ids),
    ).all() if candidate_ids else []

    scored = []
    query_token_set = set(tokens)
    for chunk in candidates:
        document = chunk.document
        combined = _normalized(f'{document.title} {chunk.heading or ""} {chunk.content}')
        combined_tokens = set(re.findall(r'[0-9a-zа-яё]{2,}', combined))
        covered = sum(
            1 for token in query_token_set
            if token in combined_tokens or any(item.startswith(_prefix(token)) for item in combined_tokens)
        )
        coverage = covered / max(len(query_token_set), 1)
        title_heading = _normalized(f'{document.title} {chunk.heading or ""}')
        trigram = max(
            _trigram_containment(normalized_query, title_heading),
            _trigram_containment(normalized_query, combined[:2_400]),
        )
        phrase = 1.0 if normalized_query in combined else 0.0
        title_match = sum(1 for item in prefixes if item in title_heading) / max(len(prefixes), 1)
        fts_bonus = (
            1.0 / (1.0 + fts_positions[chunk.id]) if chunk.id in fts_positions else 0.0
        )
        score = (
            0.47 * coverage + 0.23 * trigram + 0.16 * phrase
            + 0.09 * title_match + 0.05 * fts_bonus
        )
        if coverage or trigram >= 0.18 or phrase:
            scored.append((score, chunk))
    scored.sort(key=lambda item: (-item[0], item[1].document_id, item[1].ordinal))

    selected = []
    per_document = defaultdict(int)
    for score, chunk in scored:
        if per_document[chunk.document_id] >= 2:
            continue
        selected.append((score, chunk))
        per_document[chunk.document_id] += 1
        if len(selected) >= limit:
            break

    hits = []
    context_parts = []
    used_chars = 0
    for index, (score, chunk) in enumerate(selected, 1):
        document = chunk.document
        citation_id = f'K{index}'
        header = (
            f'[{citation_id}] {document.title} · версия {document.version}'
            f'{" · " + chunk.heading if chunk.heading else ""}\n'
            f'Источник: {document.source_uri}\n'
        )
        remaining = max_chars - used_chars - len(header) - (2 if context_parts else 0)
        if remaining < 180:
            break
        snippet = _snippet(chunk.content, normalized_query, prefixes, min(1_050, remaining))
        block = f'{header}{snippet}'
        if context_parts:
            used_chars += 2
        used_chars += len(block)
        context_parts.append(block)
        hits.append({
            'citation_id': citation_id,
            'score': round(float(score), 6),
            'document_id': document.id,
            'chunk_id': chunk.id,
            'source_key': document.source_key,
            'source_type': document.source_type,
            'title': document.title,
            'version': document.version,
            'source_uri': document.source_uri,
            'valid_until': (
                document.valid_until.isoformat() if document.valid_until else None
            ),
            'heading': chunk.heading,
            'snippet': snippet,
            'scope': 'global' if document.seller_id is None else 'seller',
        })

    return {
        'query': query,
        'hits': hits,
        'citations': [{
            key: hit[key] for key in (
                'citation_id', 'title', 'version', 'source_uri', 'heading',
                'source_key', 'valid_until',
            )
        } for hit in hits],
        'context': '\n\n'.join(context_parts),
        'context_chars': used_chars,
        'has_results': bool(hits),
        'retrieval': {
            'mode': 'fts_prefix_trigram' if fts_used else 'prefix_trigram',
            'candidate_count': len(candidates),
            'limit': limit,
            'max_chars': max_chars,
        },
    }


def evaluate_retrieval(cases: list[dict], seller_id: int, limit: int = 6) -> dict:
    """Small offline recall/MRR harness for curated JSON evaluation sets."""
    if not isinstance(cases, list) or not cases:
        raise ValueError('evaluation dataset must be a non-empty list')
    details = []
    reciprocal_sum = 0.0
    hits = 0
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            raise ValueError(f'case {index} must be an object')
        query = str(case.get('query') or '').strip()
        expected = str(case.get('expected_source_key') or '').strip()
        if not query or not expected:
            raise ValueError(f'case {index} needs query and expected_source_key')
        result = search_knowledge(seller_id=seller_id, query=query, limit=limit)
        source_keys = [hit['source_key'] for hit in result['hits']]
        rank = source_keys.index(expected) + 1 if expected in source_keys else None
        if rank:
            hits += 1
            reciprocal_sum += 1.0 / rank
        details.append({'query': query, 'expected_source_key': expected, 'rank': rank})
    count = len(details)
    return {
        'cases': count,
        f'recall_at_{limit}': hits / count,
        'mrr': reciprocal_sum / count,
        'details': details,
    }
