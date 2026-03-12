from flask import render_template, jsonify, request, redirect, url_for, flash
from flask_login import login_required, current_user
import logging
from collections import defaultdict

from models import db, WBFeedback

logger = logging.getLogger(__name__)

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


def _classify_sentiment(rating):
    if rating >= 4:
        return 'positive'
    elif rating == 3:
        return 'neutral'
    else:
        return 'negative'


def _extract_topics(text):
    """Extract topic keywords from text."""
    if not text:
        return []
    lower = text.lower()
    found = []
    for topic, keywords in TOPIC_KEYWORDS.items():
        for kw in keywords:
            if kw in lower:
                found.append(topic)
                break
    return found


def _compute_sentiment_from_db(seller_id):
    """Compute sentiment analytics from local DB (wb_feedbacks table)."""
    feedbacks = WBFeedback.query.filter(
        WBFeedback.seller_id == seller_id
    ).all()

    total = len(feedbacks)
    sentiment_counts = {'positive': 0, 'neutral': 0, 'negative': 0}

    product_stats = defaultdict(lambda: {
        'positive': 0, 'neutral': 0, 'negative': 0,
        'name': '', 'brand': '', 'total': 0,
        'topics': defaultdict(int)
    })

    topic_counts = defaultdict(int)
    topic_sentiment = defaultdict(lambda: {'positive': 0, 'neutral': 0, 'negative': 0})
    monthly_stats = defaultdict(lambda: {'positive': 0, 'neutral': 0, 'negative': 0})
    negative_reviews = []

    for fb in feedbacks:
        rating = fb.valuation or 3
        sentiment = _classify_sentiment(rating)
        sentiment_counts[sentiment] += 1

        text = fb.text or ''
        topics = _extract_topics(text)

        # Product stats
        nm_id = fb.nm_id or 0
        if nm_id:
            ps = product_stats[nm_id]
            ps[sentiment] += 1
            ps['total'] += 1
            ps['name'] = fb.product_name or ps['name']
            ps['brand'] = fb.brand_name or ps['brand']
            for t in topics:
                ps['topics'][t] += 1

        for t in topics:
            topic_counts[t] += 1
            topic_sentiment[t][sentiment] += 1

        # Monthly trend
        if fb.created_date:
            month_key = fb.created_date.strftime('%Y-%m')
            monthly_stats[month_key][sentiment] += 1

        # Collect negative reviews
        if sentiment == 'negative':
            negative_reviews.append({
                'date': fb.created_date.strftime('%Y-%m-%d') if fb.created_date else '',
                'rating': rating,
                'text': text[:200],
                'productName': fb.product_name or '',
                'nmId': nm_id,
                'userName': fb.user_name or '',
            })

    negative_reviews.sort(key=lambda x: x['date'], reverse=True)
    recent_negative = negative_reviews[:20]

    distribution = {}
    for s in ['positive', 'neutral', 'negative']:
        cnt = sentiment_counts[s]
        distribution[s] = {
            'count': cnt,
            'percent': round(cnt / total * 100, 1) if total > 0 else 0
        }

    worst_products = []
    for nm_id, ps in sorted(product_stats.items(), key=lambda x: x[1]['negative'], reverse=True)[:20]:
        if ps['negative'] > 0:
            top_topic = max(ps['topics'], key=ps['topics'].get) if ps['topics'] else ''
            worst_products.append({
                'nmId': nm_id,
                'name': ps['name'],
                'brand': ps['brand'],
                'total': ps['total'],
                'negative': ps['negative'],
                'negativePercent': round(ps['negative'] / ps['total'] * 100, 1) if ps['total'] > 0 else 0,
                'topTopic': top_topic,
            })

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

    trend = []
    for month_key in sorted(monthly_stats.keys()):
        ms = monthly_stats[month_key]
        trend.append({
            'month': month_key,
            'positive': ms['positive'],
            'neutral': ms['neutral'],
            'negative': ms['negative'],
        })

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


def register_sentiment_routes(app):
    """Register sentiment analysis routes."""

    @app.route('/sentiment')
    @login_required
    def sentiment_page():
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            flash('Для анализа отзывов необходимо настроить API ключ WB', 'warning')
            return redirect(url_for('api_settings'))
        return render_template('sentiment.html')

    @app.route('/api/sentiment/data')
    @login_required
    def api_sentiment_data():
        """Get sentiment analytics from local DB."""
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'API ключ WB не настроен'}), 403

        try:
            data = _compute_sentiment_from_db(current_user.seller.id)
            return jsonify(data)
        except Exception as e:
            logger.error(f"Error in sentiment analytics: {e}")
            return jsonify({'error': str(e)}), 500

    @app.route('/api/sentiment/status')
    @login_required
    def api_sentiment_status():
        """Backward compat — data always ready from DB."""
        if not current_user.seller:
            return jsonify({'error': 'No seller'}), 403
        data = _compute_sentiment_from_db(current_user.seller.id)
        data['_cached'] = True
        data['_stale'] = False
        data['_loading'] = False
        return jsonify(data)
