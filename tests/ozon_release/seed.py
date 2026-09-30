"""New synthetic database only; no copies of seller or provider data."""
from datetime import datetime, timedelta
import hashlib
import json

from models import (db, User, Seller, Marketplace, SellerMarketplaceAccount,
                    MarketplaceTaxonomyCategory, MarketplaceProductType,
                    MarketplaceAttributeDefinition, MarketplaceListing,
                    MarketplaceQualityAssessment, ImportedProduct)
from services.marketplace_quality import QUALITY_DEFINITION_VERSION
from services.marketplace_drafts import MarketplaceDraftService

PHOTO = 'https://ozon-fixture.test/product.svg'
USERNAME = 'ozon-ci-seller'
PASSWORD = 'synthetic-ci-password'
CATEGORIES = ['Посуда', 'Текстиль', 'Канцелярия', 'Аксессуары', 'Хранение', 'Освещение']


def seed(app):
    with app.app_context():
        db.create_all()
        assert User.query.count() == 0, 'Requires a new synthetic DB'
        now = datetime.utcnow()
        users = [User(username=name, email=name+'@example.test', is_active=True)
                 for name in [USERNAME, 'foreign-ci-seller']]
        for user in users:
            user.set_password(PASSWORD)
        sellers = [Seller(user=user, company_name='Синтетический магазин '+str(i)) for i, user in enumerate(users)]
        market = Marketplace(code='ozon', name='Ozon', adapter_code='ozon', is_active=True,
                             categories_synced_at=now, categories_snapshot_hash='synthetic-tree')
        db.session.add_all([*sellers, market]); db.session.flush()
        accounts = [SellerMarketplaceAccount(seller_id=seller.id, marketplace_id=market.id,
                    external_account_id=str(100001+i), label='Ozon CI '+str(i), is_active=True,
                    connection_status='connected') for i, seller in enumerate(sellers)]
        for i, account in enumerate(accounts):
            account.set_credentials({'client_id':str(100001+i), 'api_key':'synthetic-key-not-valid-at-provider'})
        db.session.add_all(accounts); db.session.flush()
        types = []
        for i, name in enumerate(CATEGORIES):
            category = MarketplaceTaxonomyCategory(marketplace_id=market.id, external_category_id=str(100+i),
                        name=name, full_path=name, is_available=True, last_seen_at=now)
            db.session.add(category); db.session.flush()
            product_type = MarketplaceProductType(marketplace_id=market.id, category_id=category.id,
                        external_type_id=str(1000+i), name=name, is_available=True, is_enabled=True,
                        attributes_synced_at=now, attributes_sync_status='success',
                        attributes_schema_hash='synthetic-schema', attributes_version=1,
                        attributes_count=1, required_attributes_count=0)
            db.session.add(product_type); db.session.flush(); types.append(product_type)
            db.session.add(MarketplaceAttributeDefinition(marketplace_id=market.id,
                        product_type_id=product_type.id, external_attribute_id='4191', name='Аннотация',
                        data_type='String', is_required=False, max_value_count=1, is_available=True,
                        is_enabled=True, last_seen_at=now))
        listing_ids = []
        category_examples = []
        for i in range(36):
            kind = i % len(types)
            fingerprint = hashlib.sha256(f'synthetic-listing-{i}'.encode()).hexdigest()
            listing = MarketplaceListing(seller_id=sellers[0].id, marketplace_id=market.id,
                        account_id=accounts[0].id, product_type_id=types[kind].id,
                        offer_id=f'CI-{i:03}', external_product_id=str(900000+i), primary_sku=str(800000+i),
                        external_category_id=str(100+kind), external_type_id=str(1000+kind),
                        title=f'{CATEGORIES[kind]} — '+('ЧЁРНЫЙ товар 100%_' if i==0 else 'Тестовый товар')+f' {i}',
                        description='Синтетические сведения для проверки интерфейса.',
                        normalized_status='active', is_available=True, is_archived=False,
                        media_json=json.dumps({'primary_image':PHOTO, 'images':[PHOTO]}),
                        price_summary_json=json.dumps({'currency':'RUB','available':True,
                            'values':{'old_price':'1500','price':'1000','marketing_seller_price':'900'}}),
                        stock_summary_json=json.dumps({'present':0}), sync_fingerprint=fingerprint,
                        prices_synced_at=now, info_synced_at=now, attributes_synced_at=now)
            db.session.add(listing); db.session.flush(); listing_ids.append(listing.id)
            if i < 6:
                category_examples.append({'name':CATEGORIES[kind], 'listing_id':listing.id})
            if i == 35:
                continue
            db.session.add(MarketplaceQualityAssessment(seller_id=sellers[0].id, marketplace_id=market.id,
                        account_id=accounts[0].id, listing_id=listing.id, status='scored', severity='warning',
                        score=0 if i==0 else 78, impact=100-i,
                        listing_fingerprint='b'*64 if i==1 else fingerprint,
                        definition_version=QUALITY_DEFINITION_VERSION,
                        evaluated_at=now-timedelta(days=2) if i==2 else now,
                        reasons_json=json.dumps([{'code':'ozon_few_media'},{'code':'ozon_no_analytics_signal'}]),
                        breakdown_json=json.dumps({'media':{'score':30,'hint':'Изображений: 1; ориентир — 5+'}}),
                        metrics_json='{}'))
        foreign = MarketplaceListing(seller_id=sellers[1].id, marketplace_id=market.id,
                    account_id=accounts[1].id, offer_id='FOREIGN-SECRET-OFFER', external_product_id='999999',
                    title='Foreign private title', normalized_status='active', sync_fingerprint='f'*64)
        source = ImportedProduct(seller_id=sellers[0].id, external_id='ci-source', external_vendor_code='CI-DRAFT',
                    source_type='synthetic', title='Тестовый товар для подготовки', category=CATEGORIES[0],
                    description='Сохранённое описание тестового товара.', photo_urls=json.dumps([PHOTO]))
        db.session.add_all([foreign, source]); db.session.commit()
        draft = MarketplaceDraftService.create_draft(seller_id=sellers[0].id, account_id=accounts[0].id,
                    imported_product_id=source.id, product_type_id=types[0].id)
        return {'seller_id':sellers[0].id,'account_id':accounts[0].id,
                'foreign_account_id':accounts[1].id,'foreign_listing_id':foreign.id,
                'listing_ids':listing_ids,'categories':category_examples,'draft_id':draft.id,
                'source_id':source.id}
