# -*- coding: utf-8 -*-
"""
Тест: предложение стандартных фото в маршруте /proposal карт-качества.

Проверяет, что при наличии глобального правила ProductDefaults с медиа-элементом
(position='first', mode='pin') маршрут /proposal возвращает proposal['photos']
с правильной структурой: proposed — composed-URL список, dimension='photos',
source='standard-photos'.
"""
import json
import unittest
from unittest.mock import patch, MagicMock


def _user(has_key=True):
    seller = MagicMock()
    seller.id = 7
    seller.has_valid_api_key.return_value = has_key
    u = MagicMock()
    u.is_authenticated = True
    u.seller = seller
    return u


class StandardPhotosProposalTest(unittest.TestCase):
    def setUp(self):
        import os
        os.environ.setdefault('SECRET_KEY', 'test-secret-key-for-unit-tests')
        os.environ.setdefault('DISABLE_SECURE_COOKIE', '1')
        import seller_platform as app_module
        self.app = app_module.app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()

    # ------------------------------------------------------------------
    # Основной тест: proposal/photos появляется в ответе /proposal
    # ------------------------------------------------------------------
    def test_proposal_includes_photos_from_standard_photos(self):
        """
        При наличии стандартного медиа (position='first', mode='pin')
        маршрут /proposal должен вернуть proposal['photos'] с:
          - proposed[0] == публичный URL стандартного фото
          - остаток proposed — собственные фото продукта
          - dimension == 'photos'
          - source == 'standard-photos'
        """
        user = _user()

        # Продукт с одним собственным фото
        own_url = 'https://cdn.wb.ru/nm/12345/photos/big/1.jpg'
        product = MagicMock()
        product.id = 101
        product.nm_id = 555
        product.seller_id = 7
        product.subject_id = 99
        product.photos_json = json.dumps([own_url])

        # Стандартное медиа (pin → всегда ставится первым)
        std_media = [{'filename': 'logo.jpg', 'position': 'first', 'mode': 'pin',
                      'order': 0, 'type': 'photo'}]
        std_composed = ['https://example.com/media/standard/7/logo.jpg', own_url]

        with patch('routes.card_quality.current_user', user), \
             patch('flask_login.utils._get_user', return_value=user), \
             patch('routes.card_quality.Product') as MockProduct, \
             patch('routes.card_quality.build_proposal_from_tasks', return_value={}), \
             patch('routes.card_quality.get_enrichment_service') as mock_es, \
             patch('routes.card_quality.get_standard_media', return_value=std_media) as mock_gsm, \
             patch('routes.card_quality.get_min_photos', return_value=4) as mock_gmp, \
             patch('routes.card_quality.compose_card_photo_urls', return_value=std_composed) as mock_compose:

            MockProduct.query.filter_by.return_value.first.return_value = product
            mock_es.return_value.find_supplier_data.return_value = None

            resp = self.client.post('/api/card-quality/101/proposal',
                                    json={'task_ids': {}})
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertTrue(data.get('success'), msg=f'Response: {data}')

            proposal = data.get('proposal', {})
            self.assertIn('photos', proposal,
                          msg=f'proposal keys: {list(proposal.keys())}')

            photos = proposal['photos']
            self.assertEqual(photos.get('dimension'), 'photos')
            self.assertEqual(photos.get('source'), 'standard-photos')

            proposed = photos.get('proposed', [])
            self.assertIsInstance(proposed, list)
            self.assertGreater(len(proposed), 0)
            # Первый элемент — стандартное фото
            self.assertEqual(proposed[0], std_composed[0])
            # Собственное фото тоже присутствует
            self.assertIn(own_url, proposed)

            # compose_card_photo_urls вызван с правильными аргументами
            mock_compose.assert_called_once_with([own_url], std_media, 7, 4)
            mock_gsm.assert_called_once_with(7, 99)
            mock_gmp.assert_called_once_with(7)

    def test_proposal_no_photos_when_compose_returns_empty(self):
        """
        Если compose_card_photo_urls возвращает [] (нечего добавлять),
        proposal['photos'] не должен присутствовать в ответе.
        """
        user = _user()

        own_url = 'https://cdn.wb.ru/nm/12345/photos/big/1.jpg'
        product = MagicMock()
        product.id = 101
        product.nm_id = 555
        product.seller_id = 7
        product.subject_id = 99
        product.photos_json = json.dumps([own_url])

        with patch('routes.card_quality.current_user', user), \
             patch('flask_login.utils._get_user', return_value=user), \
             patch('routes.card_quality.Product') as MockProduct, \
             patch('routes.card_quality.build_proposal_from_tasks', return_value={}), \
             patch('routes.card_quality.get_enrichment_service') as mock_es, \
             patch('routes.card_quality.get_standard_media', return_value=[]), \
             patch('routes.card_quality.get_min_photos', return_value=4), \
             patch('routes.card_quality.compose_card_photo_urls', return_value=[]):

            MockProduct.query.filter_by.return_value.first.return_value = product
            mock_es.return_value.find_supplier_data.return_value = None

            resp = self.client.post('/api/card-quality/101/proposal',
                                    json={'task_ids': {}})
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            proposal = data.get('proposal', {})
            self.assertNotIn('photos', proposal)

    def test_proposal_photos_current_matches_own(self):
        """
        proposal['photos']['current'] должен содержать исходный список фото продукта.
        """
        user = _user()

        own_urls = ['https://cdn.wb.ru/nm/12345/photos/big/1.jpg',
                    'https://cdn.wb.ru/nm/12345/photos/big/2.jpg']
        product = MagicMock()
        product.id = 101
        product.nm_id = 555
        product.seller_id = 7
        product.subject_id = 99
        product.photos_json = json.dumps(own_urls)

        std_composed = ['https://example.com/media/standard/7/logo.jpg'] + own_urls

        with patch('routes.card_quality.current_user', user), \
             patch('flask_login.utils._get_user', return_value=user), \
             patch('routes.card_quality.Product') as MockProduct, \
             patch('routes.card_quality.build_proposal_from_tasks', return_value={}), \
             patch('routes.card_quality.get_enrichment_service') as mock_es, \
             patch('routes.card_quality.get_standard_media', return_value=[{'filename': 'logo.jpg',
                                                                             'position': 'first',
                                                                             'mode': 'pin',
                                                                             'order': 0,
                                                                             'type': 'photo'}]), \
             patch('routes.card_quality.get_min_photos', return_value=4), \
             patch('routes.card_quality.compose_card_photo_urls', return_value=std_composed):

            MockProduct.query.filter_by.return_value.first.return_value = product
            mock_es.return_value.find_supplier_data.return_value = None

            resp = self.client.post('/api/card-quality/101/proposal',
                                    json={'task_ids': {}})
            data = resp.get_json()
            photos = data['proposal']['photos']
            self.assertEqual(photos['current'], own_urls)

    def test_proposal_photos_handles_null_photos_json(self):
        """
        Если photos_json у продукта None, собственные фото = [].
        Если compose возвращает не-пустой список — proposal['photos'] всё равно добавляется.
        """
        user = _user()

        product = MagicMock()
        product.id = 101
        product.nm_id = 555
        product.seller_id = 7
        product.subject_id = 99
        product.photos_json = None

        std_url = 'https://example.com/media/standard/7/logo.jpg'

        with patch('routes.card_quality.current_user', user), \
             patch('flask_login.utils._get_user', return_value=user), \
             patch('routes.card_quality.Product') as MockProduct, \
             patch('routes.card_quality.build_proposal_from_tasks', return_value={}), \
             patch('routes.card_quality.get_enrichment_service') as mock_es, \
             patch('routes.card_quality.get_standard_media', return_value=[{'filename': 'logo.jpg',
                                                                             'position': 'first',
                                                                             'mode': 'pin',
                                                                             'order': 0,
                                                                             'type': 'photo'}]), \
             patch('routes.card_quality.get_min_photos', return_value=4), \
             patch('routes.card_quality.compose_card_photo_urls', return_value=[std_url]):

            MockProduct.query.filter_by.return_value.first.return_value = product
            mock_es.return_value.find_supplier_data.return_value = None

            resp = self.client.post('/api/card-quality/101/proposal',
                                    json={'task_ids': {}})
            data = resp.get_json()
            self.assertIn('photos', data['proposal'])
            self.assertEqual(data['proposal']['photos']['current'], [])
            self.assertEqual(data['proposal']['photos']['proposed'], [std_url])


if __name__ == '__main__':
    unittest.main()
