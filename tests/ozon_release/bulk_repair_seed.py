"""A full 200-row repair batch with six synthetic type/dictionary scopes."""
from datetime import datetime
import json

from models import (db, BackgroundJob, ImportedProduct, MarketplaceAttributeDefinition,
                    MarketplaceAttributeValue, MarketplaceProductType)
from services.marketplace_drafts import MarketplaceDraftService
from services.ozon_bulk_upload import OzonBulkUploadService
from tests.ozon_release.seed import seed, PHOTO


def bulk_seed(app):
    fixture=seed(app)
    with app.app_context():
        kinds=MarketplaceProductType.query.order_by(MarketplaceProductType.id).all()
        for index,kind in enumerate(kinds):
            for aid,label,dtype in [('700','Модель','String'),('701','Работает от сети','Boolean'),
                                    ('702','Объём, л','Decimal'),('703','Материал','String')]:
                field=MarketplaceAttributeDefinition(marketplace_id=kind.marketplace_id,product_type_id=kind.id,
                    external_attribute_id=aid,name=label,data_type=dtype,is_required=True,max_value_count=1,
                    is_available=True,is_enabled=True,last_seen_at=datetime.utcnow())
                if aid=='703':
                    field.dictionary_id='synthetic-material-'+str(index)
                    field.values_synced_at=datetime.utcnow();field.values_sync_status='success'
                    field.values_snapshot_hash='synthetic-values';field.values_version=1;field.values_count=1
                db.session.add(field);db.session.flush()
                if aid=='703':
                    db.session.add(MarketplaceAttributeValue(marketplace_id=kind.marketplace_id,
                        product_type_id=kind.id,attribute_id=field.id,external_value_id=str(9000+index),
                        value='Хлопок',value_normalized='хлопок',is_available=True,last_seen_at=datetime.utcnow()))
            kind.attributes_count=5;kind.required_attributes_count=4
        db.session.commit()
        items=[];sources=[];draft_ids=[]
        for index in range(200):
            kind=kinds[index%len(kinds)]
            source=ImportedProduct(seller_id=fixture['seller_id'],external_id='bulk-source-'+str(index),
                external_vendor_code=f'BULK-{index:03}',source_type='synthetic',category='Категория '+str(index%6),
                title=f'Набор для проверки {index:03}',description='Исходное описание без неподтверждённых свойств.',
                photo_urls=json.dumps([PHOTO]) if index!=199 else '[]')
            db.session.add(source);db.session.commit();sources.append(source.id)
            draft=MarketplaceDraftService.create_draft(seller_id=fixture['seller_id'],account_id=fixture['account_id'],
                imported_product_id=source.id,product_type_id=kind.id)
            draft_ids.append(draft.id)
            items.append({'imported_product_id':source.id,'draft_id':draft.id,'offer_id':draft.offer_id,
                'title':source.title,'action':'create','status':'needs_input','code':'required_attribute_missing',
                'message':'Заполните обязательные сведения.','updated_at':datetime.utcnow().isoformat()})
        now=datetime.utcnow().isoformat()
        job=BackgroundJob(job_uid='ozon-upload-'+'b'*32,seller_id=fixture['seller_id'],
            job_type=OzonBulkUploadService.JOB_TYPE,status='completed',total=200)
        progress={'version':OzonBulkUploadService.DOCUMENT_VERSION,'source':'products',
            'account_id':fixture['account_id'],'account_label':'Синтетический Ozon','created_at':now,'updated_at':now,'items':items}
        OzonBulkUploadService._store_progress(job,progress);db.session.add(job);db.session.flush()
        OzonBulkUploadService._persist(job,progress)
        return {**fixture,'repair_job':job.job_uid,'repair_drafts':draft_ids,'repair_sources':sources}
