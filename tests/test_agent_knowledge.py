# -*- coding: utf-8 -*-
"""Contracts for curated hybrid retrieval, versions and tenant scope."""
import unittest
import json
from pathlib import Path

from flask import Flask
from sqlalchemy import text

from models import db, Seller, User, AgentKnowledgeDocument
from services.agent_knowledge import (
    evaluate_retrieval, ingest_document, search_knowledge,
)


class AgentKnowledgeServiceTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        db.init_app(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        users = [
            User(username='rag-one', email='rag1@example.com', password_hash='x'),
            User(username='rag-two', email='rag2@example.com', password_hash='x'),
        ]
        db.session.add_all(users)
        db.session.flush()
        self.seller1 = Seller(user_id=users[0].id, company_name='RAG One')
        self.seller2 = Seller(user_id=users[1].id, company_name='RAG Two')
        db.session.add_all([self.seller1, self.seller2])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.session.execute(text('DROP TABLE IF EXISTS agent_knowledge_chunks_fts'))
        db.session.commit()
        db.drop_all()
        self.ctx.pop()

    def _global(self, version='2026-07-14', content=None):
        return ingest_document(
            title='Правила описания карточек Wildberries',
            source_key='wb/content/description', source_type='wb_official',
            source_uri='https://seller.wildberries.ru/instructions/description',
            version=version,
            valid_until='2099-12-31T00:00:00',
            content=content or (
                '# Описание\nОписание карточки должно быть понятным покупателю. '
                'Не добавляйте контактные данные продавца и внешние ссылки.\n\n'
                '## Проверка\nПеред публикацией проверьте фактические характеристики товара.'
            ),
        )

    def test_hybrid_search_returns_bounded_citations_and_handles_inflection(self):
        self._global()
        result = search_knowledge(
            seller_id=self.seller1.id,
            query='какие правила для описания карточки',
            limit=8, max_chars=900,
        )
        self.assertTrue(result['has_results'])
        self.assertLessEqual(len(result['hits']), 8)
        self.assertLessEqual(result['context_chars'], 900)
        self.assertEqual(result['hits'][0]['source_key'], 'wb/content/description')
        self.assertEqual(result['hits'][0]['citation_id'], 'K1')
        self.assertEqual(result['citations'][0]['version'], '2026-07-14')
        self.assertIn('[K1]', result['context'])
        self.assertIn(result['retrieval']['mode'], {'fts_prefix_trigram', 'prefix_trigram'})

    def test_seller_scope_never_leaks_between_tenants(self):
        ingest_document(
            title='Внутренняя упаковка продавца один',
            source_key='seller/packing', source_type='seller_policy',
            source_uri='seller://policies/packing', version='1',
            seller_id=self.seller1.id,
            content=(
                '# Упаковка\nДля хрупких ваз продавец использует фиолетовую '
                'ленту и двойной слой пузырчатой плёнки.'
            ),
        )
        own = search_knowledge(
            seller_id=self.seller1.id, query='фиолетовая лента для хрупких ваз',
        )
        foreign = search_knowledge(
            seller_id=self.seller2.id, query='фиолетовая лента для хрупких ваз',
        )
        self.assertEqual(own['hits'][0]['scope'], 'seller')
        self.assertFalse(foreign['has_results'])

    def test_new_version_archives_old_and_version_is_immutable(self):
        first = self._global(version='1', content=(
            '# Лимит\nСтарое правило: описание проверяется вручную перед публикацией.'
        ))
        second = self._global(version='2', content=(
            '# Лимит\nНовое правило: описание проверяется автоматически перед публикацией.'
        ))
        self.assertEqual(first['status'], 'created')
        self.assertEqual(second['status'], 'created')
        old = db.session.get(AgentKnowledgeDocument, first['document_id'])
        new = db.session.get(AgentKnowledgeDocument, second['document_id'])
        self.assertEqual(old.status, 'archived')
        self.assertEqual(new.status, 'active')
        result = search_knowledge(
            seller_id=self.seller1.id, query='как проверяется описание перед публикацией',
        )
        self.assertEqual({hit['version'] for hit in result['hits']}, {'2'})
        with self.assertRaisesRegex(ValueError, 'another checksum'):
            self._global(version='2', content=(
                '# Подмена\nЭта версия пытается заменить уже сохранённое содержимое.'
            ))

    def test_secret_like_document_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'credential'):
            ingest_document(
                title='Небезопасный документ', source_key='seller/unsafe',
                source_type='seller_policy', source_uri='seller://unsafe', version='1',
                seller_id=self.seller1.id,
                content='Инструкция оператора.\nAPI_KEY=abcdefghijklmnop123456',
            )

    def test_official_freshness_is_required_and_expired_rows_fail_closed(self):
        with self.assertRaisesRegex(ValueError, 'require valid_until'):
            ingest_document(
                title='Официальное правило без срока', source_key='wb/no-expiry',
                source_type='wb_official',
                source_uri='https://seller.wildberries.ru/instructions/no-expiry',
                version='1', content='Проверенный официальный текст без срока действия.',
            )
        created = self._global()
        document = db.session.get(AgentKnowledgeDocument, created['document_id'])
        document.valid_until = document.created_at
        db.session.commit()
        result = search_knowledge(
            seller_id=self.seller1.id, query='правила описания карточки',
        )
        self.assertFalse(result['has_results'])

    def test_offline_evaluation_reports_recall_and_mrr(self):
        self._global()
        metrics = evaluate_retrieval([{
            'query': 'можно ли указывать внешние ссылки в описании',
            'expected_source_key': 'wb/content/description',
        }], seller_id=self.seller1.id, limit=6)
        self.assertEqual(metrics['recall_at_6'], 1.0)
        self.assertEqual(metrics['mrr'], 1.0)

    def test_curated_seed_corpus_has_perfect_core_recall(self):
        root = Path(__file__).resolve().parents[1]
        ingest_document(
            title='Создание и заполнение карточки товара WB',
            source_key='wb/cards/create', source_type='wb_official',
            source_uri=(
                'https://seller.wildberries.ru/instructions/ru/kg/material/'
                'how-to-create-card?recommended=true'
            ),
            version='2026-06-25', valid_until='2099-12-31T00:00:00',
            content=(
                root / 'knowledge/curated/wb-card-creation-2026-06-25.md'
            ).read_text(encoding='utf-8'),
        )
        cases = json.loads((
            root / 'knowledge/evals/wb-card-core.json'
        ).read_text(encoding='utf-8'))
        metrics = evaluate_retrieval(cases, seller_id=self.seller1.id, limit=6)
        self.assertEqual(metrics['recall_at_6'], 1.0)
        self.assertGreaterEqual(metrics['mrr'], 0.75)


if __name__ == '__main__':
    unittest.main()
