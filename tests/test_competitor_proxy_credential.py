# -*- coding: utf-8 -*-
"""proxy_url конкурентов — credential: шифрование fail-closed, маска наружу."""
import os
import unittest

from cryptography.fernet import Fernet
from flask import Flask

from models import (
    CompetitorMonitorSettings, CompetitorProxyEncryptionError, Seller, User, db,
)


class CompetitorProxyTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            TESTING=True,
            SQLALCHEMY_DATABASE_URI='sqlite://',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        user = User(username='proxy-user', email='proxy@test.local', is_active=True)
        user.set_password('synthetic-password')
        self.seller = Seller(user=user, company_name='ProxyShop')
        db.session.add(self.seller)
        db.session.commit()
        self.key = Fernet.generate_key().decode('ascii')
        self._old_key = os.environ.get('ENCRYPTION_KEY')

    def tearDown(self):
        if self._old_key is None:
            os.environ.pop('ENCRYPTION_KEY', None)
        else:
            os.environ['ENCRYPTION_KEY'] = self._old_key
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_write_encrypts_and_read_decrypts(self):
        os.environ['ENCRYPTION_KEY'] = self.key
        s = CompetitorMonitorSettings(seller_id=self.seller.id)
        s.proxy_url = 'http://user:secret@proxy.example.com:3128'
        db.session.add(s)
        db.session.commit()
        raw = db.session.execute(db.text(
            'SELECT proxy_url FROM competitor_monitor_settings WHERE seller_id = :sid'
        ), {'sid': self.seller.id}).scalar()
        self.assertNotIn('secret', raw)
        self.assertEqual(s.proxy_url, 'http://user:secret@proxy.example.com:3128')

    def test_write_without_key_fails_closed(self):
        os.environ.pop('ENCRYPTION_KEY', None)
        s = CompetitorMonitorSettings(seller_id=self.seller.id)
        with self.assertRaises(CompetitorProxyEncryptionError):
            s.proxy_url = 'http://user:secret@proxy.example.com:3128'

    def test_legacy_plaintext_still_readable(self):
        os.environ['ENCRYPTION_KEY'] = self.key
        s = CompetitorMonitorSettings(seller_id=self.seller.id)
        db.session.add(s)
        db.session.commit()
        db.session.execute(db.text(
            "UPDATE competitor_monitor_settings SET proxy_url = :v WHERE seller_id = :sid"
        ), {'v': 'http://legacy:pw@old.example.com:8080', 'sid': self.seller.id})
        db.session.commit()
        db.session.refresh(s)
        self.assertEqual(s.proxy_url, 'http://legacy:pw@old.example.com:8080')

    def test_display_masks_credentials(self):
        os.environ['ENCRYPTION_KEY'] = self.key
        s = CompetitorMonitorSettings(seller_id=self.seller.id)
        s.proxy_url = 'http://user:secret@proxy.example.com:3128'
        d = s.proxy_display()
        self.assertTrue(d['is_set'])
        self.assertTrue(d['has_credentials'])
        self.assertEqual(d['masked'], 'http://proxy.example.com:3128')
        self.assertNotIn('secret', str(d))

    def test_to_dict_has_no_raw_proxy(self):
        os.environ['ENCRYPTION_KEY'] = self.key
        s = CompetitorMonitorSettings(seller_id=self.seller.id)
        s.proxy_url = 'http://user:secret@proxy.example.com:3128'
        data = s.to_dict()
        self.assertNotIn('proxy_url', data)
        self.assertNotIn('secret', str(data))
        self.assertEqual(data['proxy']['masked'], 'http://proxy.example.com:3128')

    def test_clear_proxy(self):
        os.environ['ENCRYPTION_KEY'] = self.key
        s = CompetitorMonitorSettings(seller_id=self.seller.id)
        s.proxy_url = 'http://proxy.example.com:3128'
        s.proxy_url = None
        self.assertIsNone(s.proxy_url)
        self.assertFalse(s.proxy_display()['is_set'])


if __name__ == '__main__':
    unittest.main()
