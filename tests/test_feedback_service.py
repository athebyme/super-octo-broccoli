# -*- coding: utf-8 -*-
"""Contracts for WB question replies and bounded bulk draft generation."""
import unittest
from pathlib import Path
from unittest.mock import Mock

import requests

from services.feedback_service import FeedbackService, FeedbackServiceError


class _Response:
    def __init__(self, status_code, content=b'', payload=None):
        self.status_code = status_code
        self.content = content
        self._payload = payload
        self.json_calls = 0

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.HTTPError(f'HTTP {self.status_code}')
            error.response = self
            raise error

    def json(self):
        self.json_calls += 1
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FeedbackServiceContractTestCase(unittest.TestCase):
    def service_with_patch(self, response):
        service = FeedbackService('synthetic-key')
        service.session = Mock()
        service.session.patch.return_value = response
        service._last_request_time = 0
        return service

    def test_answer_question_accepts_wb_204_without_json_parse(self):
        response = _Response(204)
        service = self.service_with_patch(response)

        result = service.answer_question('question-1', 'Точный ответ')

        self.assertEqual(result, {'success': True})
        self.assertEqual(response.json_calls, 0)
        service.session.patch.assert_called_once_with(
            'https://feedbacks-api.wildberries.ru/api/v1/questions',
            json={
                'id': 'question-1',
                'answer': {'text': 'Точный ответ'},
                'state': 'wbRu',
            },
            timeout=30,
        )

    def test_answer_question_accepts_another_empty_2xx(self):
        response = _Response(200, content=b'')
        service = self.service_with_patch(response)

        self.assertEqual(
            service.answer_question('question-2', 'Готово'),
            {'success': True},
        )
        self.assertEqual(response.json_calls, 0)

    def test_non_json_body_is_a_named_error_not_json_decode_noise(self):
        response = _Response(
            200, content=b'<html>proxy error</html>',
            payload=ValueError('unexpected character'),
        )
        service = self.service_with_patch(response)

        with self.assertRaisesRegex(
            FeedbackServiceError, 'ответ неизвестного формата',
        ):
            service.answer_question('question-3', 'Ответ')

    def test_wb_problem_detail_is_bounded_and_public_safe(self):
        response = _Response(
            429,
            content=b'{"detail":"rate limit"}',
            payload={'detail': 'rate limit'},
        )
        service = self.service_with_patch(response)

        with self.assertRaisesRegex(
            FeedbackServiceError, r'HTTP 429.*rate limit',
        ):
            service.answer_question('question-4', 'Ответ')

    def test_bulk_generation_template_uses_four_worker_pool(self):
        template = (
            Path(__file__).resolve().parents[1] / 'templates' / 'reviews.html'
        ).read_text(encoding='utf-8')
        bulk = template.split('async bulkGenerateAll()', 1)[1].split(
            'async bulkSendAll()', 1,
        )[0]

        self.assertIn('bulkConcurrency: 4', template)
        self.assertIn(
            'await this._runPool(indices, this.bulkConcurrency', bulk,
        )
        self.assertNotIn('for (let i = 0; i < this.items.length; i++)', bulk)
        self.assertIn('this.generateReply(idx, {silent: true})', bulk)


if __name__ == '__main__':
    unittest.main()
