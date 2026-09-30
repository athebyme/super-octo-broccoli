"""Bounded read-only quality observations; opening a page never evaluates cards."""
from contextlib import contextmanager
from datetime import datetime, timedelta
import json
import math
import time
import unicodedata
from urllib.parse import urlsplit

from sqlalchemy import and_, case, func, or_, select, true
from sqlalchemy.exc import SQLAlchemyError
from models import (db, Marketplace, SellerMarketplaceAccount, MarketplaceListing as Listing,
    MarketplaceQualityAssessment as Assessment, MarketplaceAnalyticsSync as Analytics)
from services.marketplace_analytics import MarketplaceAnalyticsService
from services.ozon_analytics_contracts import REQUEST_METRIC_DEFINITIONS, request_fingerprint
from services.marketplace_quality import (MarketplaceQualityError, MarketplaceQualityNotFound,
    MarketplaceQualityValidationError, QUALITY_DEFINITION_VERSION, REASON_DEFINITIONS, DIMENSION_WEIGHTS)

READ_SECONDS = 5
MAX_TEXT_BYTES = 8 * 1024 * 1024
JSON_LIMIT = 262144
STATES = ('unassessed','changed','outdated','observed')
METRICS = {'views':('Просмотры','count'), 'ordered_units':('Заказано единиц','count'),
    'delivered_units':('Доставлено единиц','count'), 'returned_units':('Возвращено единиц','count'),
    'cancelled_units':('Отменено единиц','count'), 'cart_conversion_percent':('Конверсия в корзину','percent')}


class QualityReadLimit(MarketplaceQualityError):
    status_code = 422
    code = 'quality_read_limit'


def _fold(value):
    return unicodedata.normalize('NFKC', str(value or '')).casefold()


@contextmanager
def _read(engine):
    with engine.connect() as connection:
        if connection.dialect.name != 'sqlite':
            raise QualityReadLimit('Эта конфигурация базы пока не поддерживает рабочий список качества.')
        raw=connection.connection.driver_connection
        timeout=raw.execute('PRAGMA busy_timeout').fetchone()[0]
        deadline=time.monotonic()+READ_SECONDS
        try:
            raw.execute('PRAGMA busy_timeout=200')
            raw.create_function('sh_quality_casefold',1,_fold,deterministic=True)
            raw.set_progress_handler(lambda:int(time.monotonic()>deadline),1000)
            connection.exec_driver_sql('BEGIN')
            yield connection
            if time.monotonic()>deadline:
                raise QualityReadLimit('Проверка заняла слишком много времени. Повторите позже.')
        except SQLAlchemyError:
            raise QualityReadLimit('Не удалось прочитать оценки целиком. Повторите позже.') from None
        finally:
            raw.set_progress_handler(None,0)
            connection.rollback()
            raw.execute(f'PRAGMA busy_timeout={int(timeout)}')
            raw.create_function('sh_quality_casefold',1,None)


def _account(connection, seller_id, account_id):
    if any(type(v) is not int or v<=0 for v in (seller_id,account_id)):
        raise MarketplaceQualityNotFound('Магазин Ozon не найден.')
    a=SellerMarketplaceAccount
    row=connection.execute(select(a.id,a.seller_id,a.marketplace_id,a.label).join(
        Marketplace,Marketplace.id==a.marketplace_id).where(a.id==account_id,
        a.seller_id==seller_id,Marketplace.code=='ozon')).mappings().first()
    if not row:raise MarketplaceQualityNotFound('Магазин Ozon не найден.')
    return dict(row)


def _scope(model, account):
    return (model.seller_id==account['seller_id'],model.marketplace_id==account['marketplace_id'],model.account_id==account['id'])


def _base(account):
    return Listing.__table__.outerjoin(Assessment.__table__,and_(Assessment.listing_id==Listing.id,*_scope(Assessment,account))).outerjoin(
        Analytics.__table__,and_(Analytics.id==Assessment.analytics_sync_id,*_scope(Analytics,account),Analytics.status=='completed', Analytics.contract_version==MarketplaceAnalyticsService.CONTRACT_VERSION, Analytics.period_code=='30d'))


def _where(account):
    return (*_scope(Listing,account),Listing.is_available.is_(True),Listing.is_archived.is_(False))


def _state(now):
    return case((Assessment.id.is_(None),'unassessed'),
        (or_(Assessment.listing_fingerprint!=Listing.sync_fingerprint,Assessment.definition_version!=QUALITY_DEFINITION_VERSION),'changed'),
        (or_(Assessment.evaluated_at.is_(None),Assessment.evaluated_at<now-timedelta(hours=24),Assessment.evaluated_at>now+timedelta(seconds=5)),'outdated'),
        else_='observed')


def _json_sql(column, fallback='[]'):
    return case((func.json_valid(column),column),else_=fallback)


def _columns(now):
    return [Listing.id.label('listing_id'),Listing.title,Listing.offer_id,Listing.primary_sku,
        func.substr(Listing.media_json,1,JSON_LIMIT+1).label('media'),
        Assessment.id.label('assessment_id'),Assessment.status,Assessment.severity,Assessment.score,
        Assessment.impact,Assessment.evaluated_at,_state(now).label('state'),
        func.substr(Assessment.reasons_json,1,JSON_LIMIT+1).label('reasons'),
        func.substr(Assessment.breakdown_json,1,JSON_LIMIT+1).label('breakdown'),
        func.substr(Assessment.metrics_json,1,JSON_LIMIT+1).label('metrics'),
        Analytics.id.label('analytics_id'),Analytics.period_start,Analytics.period_end,Analytics.completed_at,Analytics.request_fingerprint]


def _decode(value, fallback):
    if value is None:return fallback
    if len(value.encode('utf-8'))>JSON_LIMIT:
        raise QualityReadLimit('Сохранённая оценка слишком велика. Запустите пересчёт или обратитесь в поддержку.')
    try:parsed=json.loads(value)
    except (ValueError,TypeError):return fallback
    return parsed if type(parsed) is type(fallback) else fallback


def _number(value, maximum=None):
    return value if type(value) in (int,float) and math.isfinite(value) and value>=0 and (maximum is None or value<=maximum) else None


def _instant(value):
    return value.isoformat()+'Z' if value else None


def _image(media):
    value=media.get('primary_image')
    if not value:
        images=media.get('images');value=next((x for x in images[:100] if isinstance(x,str)),None) if isinstance(images,list) else None
    try:
        parsed=urlsplit(value) if isinstance(value,str) and len(value)<=2000 else None
        return value if parsed and parsed.scheme in {'https','http'} and parsed.hostname and not parsed.username and not parsed.password else None
    except ValueError:return None


def _document(row, account, *, detail=False):
    reasons=[];seen=set()
    for value in _decode(row['reasons'],[])[:50]:
        if not isinstance(value,dict) or value.get('code') not in REASON_DEFINITIONS:continue
        code=value['code']
        if code in seen:continue
        seen.add(code);label,severity,impact=REASON_DEFINITIONS[code]
        reasons.append({'code':code,'label':label,'severity':severity})
    result={'listing_id':row['listing_id'],'account_id':account['id'],'entity_kind':'marketplace_listing','marketplace_code':'ozon',
        'title':row['title'],'offer_id':row['offer_id'],'primary_sku':row['primary_sku'],
        'image':_image(_decode(row['media'],{})),
        'url':f"/marketplaces/listings/view/{row['listing_id']}?account_id={account['id']}",
        'assessment_id':row['assessment_id'],'status':row['status'],'state':row['state'],
        'severity':row['severity'],'score':_number(row['score'],100),'priority':_number(row['impact']),
        'evaluated_at':_instant(row['evaluated_at']),'reasons':reasons}
    if detail:
        result['breakdown']={}
        for name,value in _decode(row['breakdown'],{}).items():
            if name in DIMENSION_WEIGHTS and isinstance(value,dict) and _number(value.get('score'),100) is not None:
                result['breakdown'][name]={'score':value['score'],'hint':str(value.get('hint') or '')[:500]}
        metrics=_decode(row['metrics'],{}).get('values',{});metrics=metrics if isinstance(metrics,dict) else {}
        valid_analytics=bool(row['analytics_id'] and row['period_start'] and row['period_end'] and row['completed_at'] and row['evaluated_at']
            and (row['period_end']-row['period_start']).days==29 and row['period_end']<=row['evaluated_at'].date()
            and row['completed_at']<=row['evaluated_at']+timedelta(seconds=5)
            and row['request_fingerprint']==request_fingerprint(period_start=row['period_start'],period_end=row['period_end']))
        result['metrics']=[{'code':code,'label':label,'unit':unit,
            'value':_number(metrics.get(code)) if valid_analytics and code in {m.metric_code for m in REQUEST_METRIC_DEFINITIONS} else None} for code,(label,unit) in METRICS.items()]
        result['analytics_snapshot']=({'period_start':row['period_start'].isoformat(),'period_end':row['period_end'].isoformat(),
            'completed_at':_instant(row['completed_at'])} if valid_analytics else None)
    return result


def _summary(connection, account, now):
    state=_state(now)
    summary=connection.execute(select(func.count(Listing.id).label('total'),func.count(Assessment.id).label('assessed'),
        func.avg(Assessment.score).label('average_saved_score'),func.min(Assessment.evaluated_at).label('oldest_evaluated_at'),
        func.max(Assessment.evaluated_at).label('latest_evaluated_at'),
        *[func.sum(case((state==name,1),else_=0)).label(name) for name in STATES]
    ).select_from(_base(account)).where(*_where(account))).mappings().one()
    result=dict(summary)
    result['average_saved_score']=round(result['average_saved_score'],1) if result['average_saved_score'] is not None else None
    for field in ('oldest_evaluated_at','latest_evaluated_at'):result[field]=_instant(result[field])
    for field in STATES:result[field]=int(result[field] or 0)
    reasons=func.json_each(_json_sql(Assessment.reasons_json)).table_valued('value').alias('quality_reason')
    code=func.json_extract(_json_sql(reasons.c.value,'{}'),'$.code')
    rows=connection.execute(select(code.label('code'),func.count(func.distinct(Listing.id)).label('count')).select_from(
        _base(account).join(reasons,true())
    ).where(*_where(account),code.in_(tuple(REASON_DEFINITIONS))).group_by(code)).mappings().all()
    result['reasons']=[{'code':r['code'],'count':r['count'],'label':REASON_DEFINITIONS[r['code']][0]} for r in rows]
    return result


def workspace(*, seller_id, account_id, page=1, per_page=25, severity='', reason='', state='', search='', sort_by='priority', sort_dir='desc', engine=None, now=None):
    if type(page) is not int or not 1<=page<=100000 or type(per_page) is not int or not 1<=per_page<=100:
        raise MarketplaceQualityValidationError('Некорректная страница.')
    if severity not in ('','critical','warning','good','excellent') or reason and reason not in REASON_DEFINITIONS or state not in ('',*STATES):
        raise MarketplaceQualityValidationError('Неизвестный фильтр качества.')
    if type(search) is not str or len(search)>200 or sort_by not in ('priority','score','evaluated_at','title') or sort_dir not in ('asc','desc'):
        raise MarketplaceQualityValidationError('Некорректный поиск или сортировка.')
    now=now or datetime.utcnow();search=search.strip()
    with _read(engine or db.engine) as connection:
        account=_account(connection,seller_id,account_id);conditions=list(_where(account))
        if severity:conditions.append(Assessment.severity==severity)
        if state:conditions.append(_state(now)==state)
        if reason:
            items=func.json_each(_json_sql(Assessment.reasons_json)).table_valued('value').alias('filter_reason')
            conditions.append(select(1).select_from(items).where(func.json_extract(_json_sql(items.c.value,'{}'),'$.code')==reason).exists())
        if search:
            needle=_fold(search).replace('\\','\\\\').replace('%','\\%').replace('_','\\_');pattern=f'%{needle}%'
            conditions.append(or_(*[func.sh_quality_casefold(c).like(pattern,escape='\\') for c in (Listing.title,Listing.offer_id,Listing.primary_sku)]))
        base=_base(account)
        total=connection.execute(select(func.count(Listing.id)).select_from(base).where(*conditions)).scalar_one()
        sort={'priority':Assessment.impact,'score':Assessment.score,'evaluated_at':Assessment.evaluated_at,'title':Listing.title}[sort_by]
        rows=connection.execute(select(*_columns(now)).select_from(base).where(*conditions).order_by(
            sort.asc().nullslast() if sort_dir=='asc' else sort.desc().nullslast(),Listing.id
        ).limit(per_page).offset((page-1)*per_page)).mappings().all()
        if sum(len(v.encode('utf-8')) for r in rows for v in r.values() if isinstance(v,str))>MAX_TEXT_BYTES:
            raise QualityReadLimit('Слишком много данных. Уменьшите размер страницы или уточните поиск.')
        data={'scope':{'account_id':account_id,'marketplace_code':'ozon'},'items':[_document(r,account) for r in rows],
            'summary':_summary(connection,account,now),'filters':{'severity':severity,'reason':reason,'state':state,'search':search,'sort_by':sort_by,'sort_dir':sort_dir},
            'pagination':{'page':page,'per_page':per_page,'total':total,'pages':math.ceil(total/per_page)},'observed_at':_instant(now)}
    return data


def detail(*, seller_id, account_id, listing_id, engine=None, now=None):
    if type(listing_id) is not int or listing_id<=0:raise MarketplaceQualityNotFound('Карточка не найдена.')
    now=now or datetime.utcnow()
    with _read(engine or db.engine) as connection:
        account=_account(connection,seller_id,account_id)
        row=connection.execute(select(*_columns(now)).select_from(_base(account)).where(*_where(account),Listing.id==listing_id)).mappings().first()
        if not row:raise MarketplaceQualityNotFound('Карточка недоступна в этом магазине.')
        result=_document(row,account,detail=True)
    return result
