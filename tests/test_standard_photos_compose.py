# tests/test_standard_photos_compose.py
# -*- coding: utf-8 -*-
import os
import tempfile
import unittest
from services.standard_photos import (
    compose_card_photo_paths,
    compose_card_photo_urls,
    public_media_url,
)


def m(fn, position, mode, order=0):
    return {'filename': fn, 'type': 'photo', 'position': position, 'mode': mode, 'order': order}


class ComposeTest(unittest.TestCase):
    def setUp(self):
        self.own = ['https://wb/own1.jpg', 'https://wb/own2.jpg']  # 2 свои (sparse при min=4)

    def test_pin_always_added_even_when_not_sparse(self):
        own = ['o1', 'o2', 'o3', 'o4', 'o5']  # не sparse при min=4
        media = [m('banner.jpg', 'first', 'pin')]
        res = compose_card_photo_urls(own, media, seller_id=1, min_photos=4)
        self.assertTrue(res[0].endswith('banner.jpg'))
        self.assertEqual(res[1:], own)

    def test_fill_only_when_sparse(self):
        media = [m('size.jpg', 'last', 'fill')]
        # sparse (2 < 4) → добавляется
        res = compose_card_photo_urls(self.own, media, 1, 4)
        self.assertTrue(res[-1].endswith('size.jpg'))
        # не sparse → не добавляется
        own_full = ['o1', 'o2', 'o3', 'o4']
        self.assertEqual(compose_card_photo_urls(own_full, media, 1, 4), [])

    def test_order_first_own_last(self):
        media = [m('b.jpg', 'first', 'pin', 0), m('s.jpg', 'last', 'pin', 0)]
        res = compose_card_photo_urls(self.own, media, 1, 4)
        self.assertTrue(res[0].endswith('b.jpg'))
        self.assertEqual(res[1:3], self.own)
        self.assertTrue(res[-1].endswith('s.jpg'))

    def test_order_within_group_sorted(self):
        media = [m('b2.jpg', 'first', 'pin', 2), m('b1.jpg', 'first', 'pin', 1)]
        res = compose_card_photo_urls(self.own, media, 1, 4)
        self.assertTrue(res[0].endswith('b1.jpg'))
        self.assertTrue(res[1].endswith('b2.jpg'))

    def test_dedup_and_cap_30(self):
        media = [m(f'p{i}.jpg', 'last', 'pin', i) for i in range(40)]
        res = compose_card_photo_urls(self.own, media, 1, 4)
        self.assertEqual(len(res), 30)

    def test_returns_empty_when_nothing_added(self):
        self.assertEqual(compose_card_photo_urls(self.own, [], 1, 4), [])

    def test_skips_non_photo(self):
        media = [{'filename': 'v.mp4', 'type': 'video', 'position': 'first', 'mode': 'pin', 'order': 0}]
        self.assertEqual(compose_card_photo_urls(self.own, media, 1, 4), [])

    def test_min_photos_zero_means_never_fill(self):
        fill = m('f.jpg', 'last', 'fill')
        pin = m('p.jpg', 'first', 'pin')
        # min_photos=0 → never sparse → fill not added
        self.assertEqual(compose_card_photo_urls([], [fill], 1, 0), [])
        # pin still added (it's always added when sparse would be true, but here we're checking pin works even at 0)
        res = compose_card_photo_urls([], [pin], 1, 0)
        self.assertTrue(res and res[0].endswith('p.jpg'))

    def test_multipart_paths_preserve_order_and_reject_unsafe_files(self):
        with tempfile.TemporaryDirectory() as media_dir:
            first = os.path.join(media_dir, 'first.jpg')
            last = os.path.join(media_dir, 'last.jpg')
            with open(first, 'wb') as file_obj:
                file_obj.write(b'first')
            with open(last, 'wb') as file_obj:
                file_obj.write(b'last')

            own = ['/cache/supplier-1.jpg', '/cache/supplier-2.jpg']
            media = [
                m('last.jpg', 'last', 'pin'),
                m('first.jpg', 'first', 'pin'),
                m('../outside.jpg', 'first', 'pin'),
                m('missing.jpg', 'last', 'pin'),
            ]
            result = compose_card_photo_paths(own, media, media_dir, 4)

        self.assertEqual(result[0], os.path.realpath(first))
        self.assertEqual(result[1:3], [os.path.realpath(path) for path in own])
        self.assertEqual(result[-1], os.path.realpath(last))
        self.assertEqual(len(result), 4)
