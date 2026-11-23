"""A full 200-row repair batch with six synthetic type/dictionary scopes."""
from datetime import datetime
import json

from models import (db, BackgroundJob, ImportedProduct, MarketplaceAttributeDefinition,
                    MarketplaceAttributeValue, MarketplaceProductType, Supplier,
                    SupplierProduct)
from services.marketplace_drafts import MarketplaceDraftService
from services.ozon_bulk_upload import OzonBulkUploadService
from tests.ozon_release.seed import seed, PHOTO


# These three real draft rows are reserved for the supplier-evidence browser
# journey.  Their provider attribute and dictionary identities are the exact
# Ozon cache contracts; dictionary display values and external value IDs remain
# synthetic test data.
SOURCE_REVIEW_CASES = {
    196: {
        "source": {"colors": ["Розовый", "Белый"]},
        "current": {"10096": ["Синий", "Зелёный"], "4389": "Китай"},
        "proposal": {"10096": ["Белый", "Розовый"], "4389": "Россия"},
    },
    197: {
        "source": {"colors": ["Розовый"], "country": "Россия"},
        "current": {"10096": "Синий", "4389": "Россия"},
        "proposal": {"10096": "Черный", "4389": "Россия"},
    },
    198: {
        "source": {"colors": ["Зелёный"], "country": "Беларусь"},
        "current": {"10096": "Синий", "4389": "Россия"},
        "proposal": {"10096": "Белый", "4389": "Беларусь"},
    },
}

SOURCE_REVIEW_TYPE_INDICES = {index % 6 for index in SOURCE_REVIEW_CASES}

SOURCE_REVIEW_DICTIONARY_VALUES = {
    "10096": ["Черный", "Синий", "Розовый", "Зелёный", "Белый"],
    "4389": ["Россия", "Китай", "Беларусь"],
}
SOURCE_REVIEW_VALUE_ORDINALS = {
    value: index + 1
    for values in SOURCE_REVIEW_DICTIONARY_VALUES.values()
    for index, value in enumerate(values)
}


def _source_value_id(attribute_id, type_index, value):
    attribute_offset = 0 if attribute_id == "10096" else 1_000
    return str(910_000 + type_index * 10_000 + attribute_offset
               + SOURCE_REVIEW_VALUE_ORDINALS[value])


def _set_draft_attribute(draft, attribute_id, cells):
    attributes = json.loads(draft.attributes_json or "[]")
    row = next((item for item in attributes
                if item.get("attribute_id") == attribute_id
                and item.get("complex_id", "0") == "0"), None)
    if row is None:
        row = {"attribute_id": attribute_id, "complex_id": "0", "values": []}
        attributes.append(row)
    row["values"] = cells
    draft.attributes_json = json.dumps(
        attributes, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def bulk_seed(app):
    fixture=seed(app)
    from services.ozon_reference_service import OzonReferenceService
    from services.supplier_service import SupplierCSVParser

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

        review_supplier=Supplier(
            name="Synthetic source review supplier",
            code="synthetic-source-review",
            is_active=True,
            csv_column_mapping={
                "colors": {"column": "colors", "type": "list", "separator": "|"},
                "country": {"column": "country", "type": "string"},
            },
        )
        db.session.add(review_supplier);db.session.flush()

        for kind_index in sorted(SOURCE_REVIEW_TYPE_INDICES):
            kind=kinds[kind_index]
            for attribute_id,name,dictionary_id,is_collection,max_value_count in (
                ("10096","Цвет товара","1494",True,0),
                ("4389","Страна-изготовитель","1935",True,2),
            ):
                values=SOURCE_REVIEW_DICTIONARY_VALUES[attribute_id]
                attribute=MarketplaceAttributeDefinition(
                    marketplace_id=kind.marketplace_id,
                    product_type_id=kind.id,
                    external_attribute_id=attribute_id,
                    name=name,
                    data_type="String",
                    is_required=False,
                    dictionary_id=dictionary_id,
                    max_value_count=max_value_count,
                    is_collection=is_collection,
                    is_available=True,
                    is_enabled=True,
                    last_seen_at=datetime.utcnow(),
                    values_synced_at=datetime.utcnow(),
                    values_sync_status="success",
                    values_snapshot_hash=f"synthetic-ozon-{dictionary_id}-{kind_index}",
                    values_version=1,
                    values_count=len(values),
                )
                db.session.add(attribute);db.session.flush()
                for value in values:
                    external_value_id=(
                        _source_value_id(attribute_id,kind_index,value)
                    )
                    db.session.add(MarketplaceAttributeValue(
                        marketplace_id=kind.marketplace_id,
                        product_type_id=kind.id,
                        attribute_id=attribute.id,
                        external_value_id=external_value_id,
                        value=value,
                        value_normalized=OzonReferenceService.normalize_value(value),
                        is_available=True,
                        last_seen_at=datetime.utcnow(),
                    ))
            # The base fixture has five exact definitions. These two optional
            # cache-backed fields are part of the real schema snapshot too.
            kind.attributes_count=7
        db.session.commit()
        items=[];sources=[];draft_ids=[];supplier_source_drafts={}
        for index in range(200):
            kind=kinds[index%len(kinds)]
            external_id='bulk-source-'+str(index)
            supplier_product=None
            source_snapshot_json=None
            source_case=SOURCE_REVIEW_CASES.get(index)
            if source_case:
                feed_row={
                    "external_id":external_id,
                    "vendor_code":f"BULK-{index:03}",
                    "title":f"Набор для проверки {index:03}",
                    "category":"Категория "+str(index%6),
                    "colors":list(source_case["source"]["colors"]),
                }
                if "country" in source_case["source"]:
                    feed_row["country"]=source_case["source"]["country"]
                observed=SupplierCSVParser._observed_source_fields(
                    feed_row,review_supplier,
                )
                assert observed==source_case["source"]
                source_snapshot={
                    **feed_row,
                    "observed_supplier_fields_version":1,
                    "observed_supplier_fields":observed,
                }
                source_snapshot_json=json.dumps(
                    source_snapshot,ensure_ascii=False,sort_keys=True,
                )
                supplier_product=SupplierProduct(
                    supplier_id=review_supplier.id,
                    external_id=external_id,
                    vendor_code=feed_row["vendor_code"],
                    title=feed_row["title"],
                    category=feed_row["category"],
                    colors_json=json.dumps(feed_row["colors"],ensure_ascii=False),
                    country=feed_row.get("country"),
                    original_data_json=source_snapshot_json,
                    content_revision=1,
                    status="ready",
                )
                db.session.add(supplier_product);db.session.flush()
            source=ImportedProduct(seller_id=fixture['seller_id'],external_id=external_id,
                external_vendor_code=f'BULK-{index:03}',category='Категория '+str(index%6),
                title=f'Набор для проверки {index:03}',description='Исходное описание без неподтверждённых свойств.',
                photo_urls=json.dumps([PHOTO]) if index!=199 else '[]',
                supplier_id=review_supplier.id if supplier_product else None,
                supplier_product_id=supplier_product.id if supplier_product else None,
                supplier_content_revision=supplier_product.content_revision if supplier_product else 0,
                source_type=review_supplier.code if supplier_product else 'synthetic',
                original_data=source_snapshot_json)
            db.session.add(source);db.session.commit();sources.append(source.id)
            draft=MarketplaceDraftService.create_draft(seller_id=fixture['seller_id'],account_id=fixture['account_id'],
                imported_product_id=source.id,product_type_id=kind.id)
            if source_case:
                for attribute_id,current_value in source_case["current"].items():
                    dictionary_id="1494" if attribute_id=="10096" else "1935"
                    values=current_value if isinstance(current_value,list) else [current_value]
                    _set_draft_attribute(
                        draft,attribute_id,
                        [{
                            "value":value,
                            "dictionary_value_id":_source_value_id(
                                attribute_id,index%6,value,
                            ),
                        } for value in values],
                    )
                supplier_source_drafts[index]=draft.id
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
        db.session.commit()
        return {**fixture,'repair_job':job.job_uid,'repair_drafts':draft_ids,'repair_sources':sources,
                'supplier_source_drafts':supplier_source_drafts,
                'supplier_source_cases':SOURCE_REVIEW_CASES}
