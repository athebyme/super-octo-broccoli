# -*- coding: utf-8 -*-
import os, unittest, tempfile
from flask import Flask


class PublicMediaRouteTest(unittest.TestCase):
    def setUp(self):
        from routes.product_defaults import register_product_defaults_routes
        self.app = Flask(__name__, root_path=tempfile.mkdtemp())
        self.app.config['TESTING'] = True
        register_product_defaults_routes(self.app)
        # положим файл
        d = os.path.join(self.app.root_path, 'data', 'global_media', '7')
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'pic.jpg'), 'wb') as f:
            f.write(b'\xff\xd8\xff\xe0JPEGDATA')
        self.client = self.app.test_client()

    def test_serves_file_without_login(self):
        r = self.client.get('/media/standard/7/pic.jpg')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'JPEGDATA', r.data)

    def test_missing_file_404(self):
        self.assertEqual(self.client.get('/media/standard/7/nope.jpg').status_code, 404)

    def test_path_traversal_blocked(self):
        r = self.client.get('/media/standard/7/..%2f..%2fmodels.py')
        self.assertIn(r.status_code, (400, 404))


if __name__ == '__main__':
    unittest.main()
