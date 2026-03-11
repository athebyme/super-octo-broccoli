from flask import render_template, jsonify, request, redirect, url_for, flash
from flask_login import login_required, current_user
import requests
import logging
import threading
import time
from collections import defaultdict

logger = logging.getLogger(__name__)

FEEDBACKS_API_URL = "https://feedbacks-api.wildberries.ru"

# In-memory cache: {seller_id: {data, updated_at, loading}}
_cache = {}
_cache_lock = threading.Lock()
CACHE_TTL = 300  # 5 minutes

# Topic keyword mapping (lowercase)
TOPIC_KEYWORDS = {
    "Качество": ["качество", "материал", "ткань"],
    "Доставка": ["доставка", "курьер", "привезли"],
    "Упаковка": ["упаковка", "коробка", "пакет"],
    "Размер": ["размер", "маломерит", "большемерит"],
    "Цена": ["цена", "дорого", "дёшево", "стоимость"],
    "Описание": ["описание", "фото", "не соответствует", "отличается"],
    "Запах": ["запах", "пахнет"],
    "Цвет": ["цвет", "оттенок"],
}


def _get_cached(seller_id):
    with _cache_lock:
        entry = _cache.get(seller_id)
        if entry and entry.get('data'):
            age = time.time() - entry.get('updated_at', 0)
            return entry['data'], age < CACHE_TTL, entry.get('loading', False)
    return None, False, False


def _set_cached(seller_id, data):
    with _cache_lock:
        _cache[seller_id] = {
            'data': data,
            'updated_at': time.time(),
            'loading': False
        }


def _set_loading(seller_id, loading=True):
    with _cache_lock:
        if seller_id not in _cache:
            _cache[seller_id] = {'data': None, 'updated_at': 0, 'loading': loading}
        else:
            _cache[seller_id]['loading'] = loading


def _is_loading(seller_id):
    with _cache_lock:
        entry = _cache.get(seller_id)
        return entry.get('loading', False) if entry else False


def _classify_sentiment(rating):
    """Rule-based sentiment from productValuation."""
    if rating >= 4:
        return 'positive'
    elif rating == 3:
        return 'neutral'
    else:
        return 'negative'


def _extract_topics(text, pros, cons):
    """Extract topic keywords from combined text fields."""
    combined = ' '.join(filter(None, [text, pros, cons])).lower()
    found = []
    for topic, keywords in TOPIC_KEYWORDS.items():
        for kw in keywords:
            if kw in combined:
                found.append(topic)
                break
    return found


def _compute_sentiment_analytics(feedbacks):
    """Compute all sentiment analytics from raw feedbacks list."""
    total = len(feedbacks)
    sentiment_counts = {'positive': 0, 'neutral': 0, 'negative': 0}

    # Per-product stats
    product_stats = defaultdict(lambda: {
        'positive': 0, 'neutral': 0, 'negative': 0,
        'name': '', 'brand': '', 'article': '', 'total': 0,
        'topics': defaultdict(int)
    })

    # Topic stats
    topic_counts = defaultdict(int)
    topic_sentiment = defaultdict(lambda: {'positive': 0, 'neutral': 0, 'negative': 0})

    # Monthly trend
    monthly_stats = defaultdict(lambda: {'positive': 0, 'neutral': 0, 'negative': 0})

    # Recent negative reviews
    negative_reviews = []

    for fb in feedbacks:
        rating = fb.get('productValuation', 3)
        sentiment = _classify_sentiment(rating)
        sentiment_counts[sentiment] += 1

        text = fb.get('text', '') or ''
        pros = fb.get('pros', '') or ''
        cons = fb.get('cons', '') or ''
        topics = _extract_topics(text, pros, cons)

        # Product stats
        details = fb.get('productDetails', {}) or {}
        nm_id = details.get('nmId', 0)
        if nm_id:
            ps = product_stats[nm_id]
            ps[sentiment] += 1
            ps['total'] += 1
            ps['name'] = details.get('productName', '') or ''
            ps['brand'] = details.get('brandName', '') or ''
            ps['article'] = details.get('supplierArticle', '') or ''
            for t in topics:
                ps['topics'][t] += 1

        # Topic stats
        for t in topics:
            topic_counts[t] += 1
            topic_sentiment[t][sentiment] += 1

        # Monthly trend
        created = fb.get('createdDate', '') or ''
        month_key = created[:7] if len(created) >= 7 else ''
        if month_key:
            monthly_stats[month_key][sentiment] += 1

        # Collect negative reviews
        if sentiment == 'negative':
            excerpt = text[:200] if text else (cons[:200] if cons else '')
            negative_reviews.append({
                'date': created[:10] if len(created) >= 10 else '',
                'rating': rating,
                'text': excerpt,
                'productName': details.get('productName', '') or '',
                'nmId': nm_id,
                'userName': fb.get('userName', '') or '',
            })

    # Sort negative reviews by date descending, take last 20
    negative_reviews.sort(key=lambda x: x['date'], reverse=True)
    recent_negative = negative_reviews[:20]

    # Sentiment distribution with percentages
    distribution = {}
    for s in ['positive', 'neutral', 'negative']:
        cnt = sentiment_counts[s]
        distribution[s] = {
            'count': cnt,
            'percent': round(cnt / total * 100, 1) if total > 0 else 0
        }

    # Worst products (most negative reviews)
    worst_products = []
    for nm_id, ps in sorted(product_stats.items(), key=lambda x: x[1]['negative'], reverse=True)[:20]:
        if ps['negative'] > 0:
            # Find top complaint topic
            top_topic = ''
            if ps['topics']:
                top_topic = max(ps['topics'], key=ps['topics'].get)
            worst_products.append({
                'nmId': nm_id,
                'name': ps['name'],
                'brand': ps['brand'],
                'article': ps['article'],
                'total': ps['total'],
                'negative': ps['negative'],
                'negativePercent': round(ps['negative'] / ps['total'] * 100, 1) if ps['total'] > 0 else 0,
                'topTopic': top_topic,
            })

    # Topic bars data
    topics_data = []
    for topic, count in sorted(topic_counts.items(), key=lambda x: x[1], reverse=True):
        ts = topic_sentiment[topic]
        topics_data.append({
            'topic': topic,
            'count': count,
            'positive': ts['positive'],
            'neutral': ts['neutral'],
            'negative': ts['negative'],
        })

    # Monthly trend sorted
    trend = []
    for month_key in sorted(monthly_stats.keys()):
        ms = monthly_stats[month_key]
        trend.append({
            'month': month_key,
            'positive': ms['positive'],
            'neutral': ms['neutral'],
            'negative': ms['negative'],
        })

    # Top negative topic overall
    top_negative_topic = ''
    if topic_sentiment:
        top_negative_topic = max(topic_sentiment.keys(), key=lambda t: topic_sentiment[t]['negative'])

    return {
        'totalReviews': total,
        'distribution': distribution,
        'topNegativeTopic': top_negative_topic,
        'topics': topics_data,
        'trend': trend,
        'worstProducts': worst_products,
        'recentNegative': recent_negative,
    }


def _fetch_all_feedbacks(api_key):
    """Fetch both answered and unanswered feedbacks from WB API."""
    session = requests.Session()
    session.headers.update({
        'Authorization': api_key,
        'Content-Type': 'application/json'
    })

    all_feedbacks = []
    for is_answered in ['false', 'true']:
        try:
            resp = session.get(
                f"{FEEDBACKS_API_URL}/api/v1/feedbacks",
                params={'isAnswered': is_answered, 'take': 5000, 'skip': 0},
                timeout=60
            )
            resp.raise_for_status()
            result = resp.json()
            feedbacks = (result.get('data') or {}).get('feedbacks') or []
            all_feedbacks.extend(feedbacks)
        except Exception as e:
            logger.error(f"Error fetching feedbacks (isAnswered={is_answered}): {e}")
            raise

    return all_feedbacks


def _fetch_and_cache(api_key, seller_id):
    """Fetch data from WB API and update cache. Runs in background thread."""
    try:
        _set_loading(seller_id, True)
        feedbacks = _fetch_all_feedbacks(api_key)
        data = _compute_sentiment_analytics(feedbacks)
        _set_cached(seller_id, data)
        logger.info(f"Sentiment cache updated for seller {seller_id}: {len(feedbacks)} feedbacks")
    except Exception as e:
        logger.error(f"Background sentiment fetch error for seller {seller_id}: {e}")
        _set_loading(seller_id, False)


def register_sentiment_routes(app):
    """Register sentiment analysis routes."""

    @app.route('/sentiment')
    @login_required
    def sentiment_page():
        """Sentiment analysis page."""
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            flash('Для анализа отзывов необходимо настроить API ключ WB', 'warning')
            return redirect(url_for('api_settings'))
        return render_template('sentiment.html')

    @app.route('/api/sentiment/data')
    @login_required
    def api_sentiment_data():
        """Get sentiment analytics data. Returns cached data instantly if available,
        triggers background refresh if stale."""
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'API ключ WB не настроен'}), 403

        try:
            seller_id = current_user.seller.id
            force = request.args.get('force', '').lower() == 'true'

            cached_data, is_fresh, is_loading = _get_cached(seller_id)

            if cached_data and not force:
                result = dict(cached_data)
                result['_cached'] = True
                result['_stale'] = not is_fresh
                result['_loading'] = is_loading

                if not is_fresh and not is_loading:
                    t = threading.Thread(
                        target=_fetch_and_cache,
                        args=(current_user.seller.wb_api_key, seller_id),
                        daemon=True
                    )
                    t.start()

                return jsonify(result)

            if is_loading:
                return jsonify({'_loading': True, '_cached': False}), 202

            if not force:
                _set_loading(seller_id, True)
                t = threading.Thread(
                    target=_fetch_and_cache,
                    args=(current_user.seller.wb_api_key, seller_id),
                    daemon=True
                )
                t.start()
                return jsonify({'_loading': True, '_cached': False}), 202

            # Force refresh — synchronous
            feedbacks = _fetch_all_feedbacks(current_user.seller.wb_api_key)
            data = _compute_sentiment_analytics(feedbacks)
            _set_cached(seller_id, data)
            return jsonify(data)

        except requests.exceptions.RequestException as e:
            logger.error(f"WB API error in sentiment: {e}")
            return jsonify({'error': f'Ошибка WB API: {str(e)}'}), 502
        except Exception as e:
            logger.error(f"Error in sentiment analytics: {e}")
            return jsonify({'error': str(e)}), 500

    @app.route('/api/sentiment/status')
    @login_required
    def api_sentiment_status():
        """Check if background data fetch is complete."""
        if not current_user.seller:
            return jsonify({'error': 'No seller'}), 403
        seller_id = current_user.seller.id
        cached_data, is_fresh, is_loading = _get_cached(seller_id)
        if cached_data:
            result = dict(cached_data)
            result['_cached'] = True
            result['_stale'] = not is_fresh
            result['_loading'] = is_loading
            return jsonify(result)
        return jsonify({'_loading': is_loading, '_cached': False}), 202 if is_loading else 200
