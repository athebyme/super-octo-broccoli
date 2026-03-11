from flask import render_template, jsonify, request, flash, redirect, url_for
from flask_login import login_required, current_user
import json
import logging
import traceback

logger = logging.getLogger(__name__)


def register_reviews_routes(app):
    """Register reviews and feedback routes."""

    @app.route('/reviews')
    @login_required
    def reviews_page():
        """Reviews and Q&A management page."""
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            flash('Для работы с отзывами необходимо настроить API ключ WB', 'warning')
            return redirect(url_for('api_settings'))
        return render_template('reviews.html')

    @app.route('/api/reviews/stats')
    @login_required
    def api_reviews_stats():
        """Get reviews and questions statistics."""
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'API ключ WB не настроен'}), 403
        try:
            from services.feedback_service import FeedbackService
            svc = FeedbackService(current_user.seller.wb_api_key)
            stats = svc.get_reputation_stats()
            return jsonify(stats)
        except Exception as e:
            logger.error(f"Error fetching review stats: {e}")
            return jsonify({'error': str(e)}), 500

    @app.route('/api/reviews/feedbacks')
    @login_required
    def api_reviews_feedbacks():
        """Get feedbacks list."""
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'API ключ WB не настроен'}), 403
        try:
            from services.feedback_service import FeedbackService
            svc = FeedbackService(current_user.seller.wb_api_key)
            is_answered = request.args.get('isAnswered', 'false').lower() == 'true'
            take = min(int(request.args.get('take', 50)), 5000)
            skip = int(request.args.get('skip', 0))
            order = request.args.get('order', 'dateDesc')
            nm_id = request.args.get('nmId', type=int)
            result = svc.get_feedbacks(
                is_answered=is_answered,
                take=take,
                skip=skip,
                nm_id=nm_id,
                order=order
            )
            return jsonify(result)
        except Exception as e:
            logger.error(f"Error fetching feedbacks: {e}")
            return jsonify({'error': str(e)}), 500

    @app.route('/api/reviews/questions')
    @login_required
    def api_reviews_questions():
        """Get questions list."""
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'API ключ WB не настроен'}), 403
        try:
            from services.feedback_service import FeedbackService
            svc = FeedbackService(current_user.seller.wb_api_key)
            is_answered = request.args.get('isAnswered', 'false').lower() == 'true'
            take = min(int(request.args.get('take', 50)), 10000)
            skip = int(request.args.get('skip', 0))
            order = request.args.get('order', 'dateDesc')
            nm_id = request.args.get('nmId', type=int)
            result = svc.get_questions(
                is_answered=is_answered,
                take=take,
                skip=skip,
                nm_id=nm_id,
                order=order
            )
            return jsonify(result)
        except Exception as e:
            logger.error(f"Error fetching questions: {e}")
            return jsonify({'error': str(e)}), 500

    @app.route('/api/reviews/generate-reply', methods=['POST'])
    @login_required
    def api_reviews_generate_reply():
        """Generate reply for a feedback or question. Uses AI if enabled, templates otherwise."""
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'API ключ WB не настроен'}), 403
        try:
            body = request.get_json(silent=True) or {}
            item_type = body.get('type', 'feedback')
            text = body.get('text', '')
            product_name = body.get('productName', '')
            rating = body.get('rating', None)
            pros = body.get('pros', '')
            cons = body.get('cons', '')
            user_name = body.get('userName', '')

            # Check if AI generation is enabled
            ai_reply = _try_ai_generate(
                current_user.seller, item_type, text, rating,
                product_name, pros, cons, user_name
            )

            if ai_reply:
                return jsonify({'reply': ai_reply, 'source': 'ai'})

            # Fallback to templates
            if item_type == 'feedback':
                reply = _generate_feedback_reply(text, rating, product_name, pros, cons, user_name)
            else:
                reply = _generate_question_reply(text, product_name, user_name)

            return jsonify({'reply': reply, 'source': 'template'})
        except Exception as e:
            logger.error(f"Error generating reply: {e}\n{traceback.format_exc()}")
            return jsonify({'error': str(e)}), 500

    @app.route('/api/reviews/settings', methods=['GET'])
    @login_required
    def api_reviews_settings_get():
        """Get review reply settings."""
        if not current_user.seller:
            return jsonify({'error': 'No seller'}), 403
        settings = _get_review_settings(current_user.seller.id)
        return jsonify(settings)

    @app.route('/api/reviews/settings', methods=['POST'])
    @login_required
    def api_reviews_settings_save():
        """Save review reply settings."""
        if not current_user.seller:
            return jsonify({'error': 'No seller'}), 403
        try:
            from models import db, SystemSettings
            body = request.get_json(silent=True) or {}
            seller_id = current_user.seller.id
            key = f'reviews_ai_settings_{seller_id}'

            settings = SystemSettings.query.filter_by(key=key).first()
            if not settings:
                settings = SystemSettings(
                    key=key,
                    value_type='json',
                    description=f'AI настройки ответов на отзывы для продавца {seller_id}'
                )
                db.session.add(settings)

            settings.set_value({
                'ai_enabled': bool(body.get('ai_enabled', False)),
                'custom_instruction': body.get('custom_instruction', ''),
                'tone': body.get('tone', 'professional'),
            })
            settings.updated_by_user_id = current_user.id
            db.session.commit()

            return jsonify({'success': True})
        except Exception as e:
            logger.error(f"Error saving review settings: {e}")
            return jsonify({'error': str(e)}), 500

    @app.route('/api/reviews/send-reply', methods=['POST'])
    @login_required
    def api_reviews_send_reply():
        """Send reply to a feedback or question via WB API."""
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'API ключ WB не настроен'}), 403
        try:
            body = request.get_json(silent=True) or {}
            item_id = body.get('id')
            text = body.get('text', '').strip()
            item_type = body.get('type', 'feedback')

            if not item_id or not text:
                return jsonify({'error': 'ID и текст обязательны'}), 400

            if len(text) < 2:
                return jsonify({'error': 'Ответ слишком короткий (минимум 2 символа)'}), 400
            if len(text) > 5000:
                return jsonify({'error': 'Ответ слишком длинный (максимум 5000 символов)'}), 400

            from services.feedback_service import FeedbackService
            svc = FeedbackService(current_user.seller.wb_api_key)

            if item_type == 'question':
                result = svc.answer_question(item_id, text)
            else:
                result = svc.answer_feedback(item_id, text)

            return jsonify({'success': True, 'result': result})
        except Exception as e:
            logger.error(f"Error sending reply: {e}")
            return jsonify({'error': str(e)}), 500

    @app.route('/api/reviews/check-new', methods=['POST'])
    @login_required
    def api_reviews_check_new():
        """Check for new unanswered feedbacks/questions and create notifications."""
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'API ключ WB не настроен'}), 403
        try:
            from services.feedback_service import FeedbackService
            from models import db, Notification

            seller = current_user.seller
            svc = FeedbackService(seller.wb_api_key)

            fb_data = svc.get_feedbacks_unanswered_count()
            q_data = svc.get_questions_unanswered_count()

            fb_unanswered = fb_data.get('data', {}).get('countUnanswered', 0)
            fb_today = fb_data.get('data', {}).get('countUnansweredToday', 0)
            q_unanswered = q_data.get('data', {}).get('countUnanswered', 0)
            q_today = q_data.get('data', {}).get('countUnansweredToday', 0)

            notifications_created = []

            # Notify about new unanswered feedbacks today
            if fb_today > 0:
                # Check if we already sent a similar notification today
                from datetime import datetime, timedelta
                today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
                existing = Notification.query.filter(
                    Notification.seller_id == seller.id,
                    Notification.category == 'warning',
                    Notification.created_at >= today_start,
                    Notification.title.like('%новых отзыв%')
                ).first()

                if not existing:
                    # Load recent negative feedbacks for details
                    negative_details = []
                    try:
                        recent = svc.get_feedbacks(is_answered=False, take=10, order='dateDesc')
                        for fb in recent.get('data', {}).get('feedbacks', []):
                            rating = fb.get('productValuation', 5)
                            if rating <= 2:
                                product = fb.get('productDetails', {}).get('productName', 'Товар')
                                negative_details.append({
                                    'rating': rating,
                                    'product': product[:50],
                                    'text': (fb.get('text', '') or '')[:100]
                                })
                    except Exception:
                        pass

                    if fb_today == 1:
                        word = 'новый отзыв'
                    elif fb_today < 5:
                        word = 'новых отзыва'
                    else:
                        word = 'новых отзывов'

                    title = f'{fb_today} {word} без ответа'
                    message = f'Всего неотвеченных отзывов: {fb_unanswered}.'
                    if negative_details:
                        message += f' Из них негативных (1-2 звезды): {len(negative_details)}.'

                    category = 'error' if negative_details else 'warning'

                    notif = Notification(
                        seller_id=seller.id,
                        category=category,
                        title=title,
                        message=message,
                        link='/reviews',
                        metadata_json=json.dumps({
                            'type': 'reviews',
                            'feedbacks_unanswered': fb_unanswered,
                            'feedbacks_today': fb_today,
                            'negative_details': negative_details[:5]
                        }, ensure_ascii=False)
                    )
                    db.session.add(notif)
                    notifications_created.append('feedbacks')

            # Notify about new unanswered questions today
            if q_today > 0:
                from datetime import datetime, timedelta
                today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
                existing = Notification.query.filter(
                    Notification.seller_id == seller.id,
                    Notification.category == 'warning',
                    Notification.created_at >= today_start,
                    Notification.title.like('%новых вопрос%')
                ).first()

                if not existing:
                    if q_today == 1:
                        word = 'новый вопрос'
                    elif q_today < 5:
                        word = 'новых вопроса'
                    else:
                        word = 'новых вопросов'

                    notif = Notification(
                        seller_id=seller.id,
                        category='warning',
                        title=f'{q_today} {word} без ответа',
                        message=f'Всего неотвеченных вопросов: {q_unanswered}.',
                        link='/reviews?tab=questions',
                        metadata_json=json.dumps({
                            'type': 'reviews_questions',
                            'questions_unanswered': q_unanswered,
                            'questions_today': q_today
                        }, ensure_ascii=False)
                    )
                    db.session.add(notif)
                    notifications_created.append('questions')

            if notifications_created:
                db.session.commit()

            return jsonify({
                'feedbacks_unanswered': fb_unanswered,
                'feedbacks_today': fb_today,
                'questions_unanswered': q_unanswered,
                'questions_today': q_today,
                'notifications_created': notifications_created
            })
        except Exception as e:
            logger.error(f"Error checking new reviews: {e}")
            return jsonify({'error': str(e)}), 500

    @app.route('/api/reviews/rating-distribution')
    @login_required
    def api_reviews_rating_distribution():
        """Get rating distribution."""
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'API ключ WB не настроен'}), 403
        try:
            from services.feedback_service import FeedbackService
            svc = FeedbackService(current_user.seller.wb_api_key)
            dist = svc.get_rating_distribution(total_count=2000)
            return jsonify(dist)
        except Exception as e:
            logger.error(f"Error fetching rating distribution: {e}")
            return jsonify({'error': str(e)}), 500


def _get_review_settings(seller_id):
    """Get review AI settings for a seller."""
    from models import SystemSettings
    key = f'reviews_ai_settings_{seller_id}'
    settings = SystemSettings.query.filter_by(key=key).first()
    if settings:
        val = settings.get_value()
        if isinstance(val, dict):
            return val
    return {'ai_enabled': False, 'custom_instruction': '', 'tone': 'professional'}


def _try_ai_generate(seller, item_type, text, rating, product_name, pros, cons, user_name):
    """Try to generate reply using AI. Returns None if AI is not configured or fails."""
    try:
        settings = _get_review_settings(seller.id)
        if not settings.get('ai_enabled'):
            return None

        # Get AI config from AutoImportSettings
        from models import AutoImportSettings
        ai_settings = AutoImportSettings.query.filter_by(seller_id=seller.id).first()
        if not ai_settings or not getattr(ai_settings, 'ai_enabled', False):
            logger.warning("AI enabled for reviews but no AI config found")
            return None

        from services.ai_service import AIConfig, AIService
        config = AIConfig.from_settings(ai_settings)
        if not config:
            return None

        config.temperature = 0.7
        config.max_tokens = 500
        ai_service = AIService(config)

        tone = settings.get('tone', 'professional')
        custom_instruction = settings.get('custom_instruction', '')

        # Build system prompt
        tone_map = {
            'professional': 'Отвечай профессионально и вежливо, как представитель солидного магазина.',
            'friendly': 'Отвечай дружелюбно и тепло, как будто общаешься с хорошим знакомым.',
            'formal': 'Отвечай строго формально и официально.',
        }
        tone_instruction = tone_map.get(tone, tone_map['professional'])

        system_prompt = f"""Ты — представитель интернет-магазина на Wildberries. Пиши ответы на отзывы и вопросы покупателей.

Правила:
- Ответ на русском языке, 2-4 предложения, не больше
- {tone_instruction}
- Для ПОЛОЖИТЕЛЬНЫХ отзывов (4-5 звёзд): поблагодари за выбор магазина, порадуйся положительному опыту
- Для НЕЙТРАЛЬНЫХ отзывов (3 звезды): поблагодари, мягко адресуй замечания, сошлись на описание товара
- Для НЕГАТИВНЫХ отзывов (1-2 звезды): ЗАЩИЩАЙ карточку товара. Товар соответствует описанию и характеристикам. Магазин прав. Вежливо порекомендуй ознакомиться с описанием перед покупкой. НЕ извиняйся за товар
- Для ВОПРОСОВ: отвечай по существу, ссылайся на описание и характеристики в карточке
- Не используй эмодзи
- Не начинай с "Уважаемый покупатель"
- Начинай с приветствия по имени если оно известно"""

        if custom_instruction:
            system_prompt += f"\n\nДополнительные инструкции от продавца:\n{custom_instruction}"

        # Build user message
        parts = []
        if item_type == 'feedback':
            parts.append(f"Тип: отзыв на товар")
            if rating:
                parts.append(f"Рейтинг: {rating} из 5 звёзд")
        else:
            parts.append(f"Тип: вопрос покупателя")

        if product_name:
            parts.append(f"Товар: {product_name}")
        if user_name:
            parts.append(f"Имя покупателя: {user_name}")
        if text:
            parts.append(f"Текст: {text}")
        if pros:
            parts.append(f"Достоинства: {pros}")
        if cons:
            parts.append(f"Недостатки: {cons}")

        user_message = "\n".join(parts)
        if not user_message.strip():
            user_message = f"Отзыв без текста, рейтинг: {rating or 'не указан'}, товар: {product_name or 'не указан'}"

        result = ai_service.chat_completion(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ]
        )

        if result and len(result.strip()) >= 10:
            return result.strip()

        return None
    except Exception as e:
        logger.error(f"AI review reply generation failed: {e}")
        return None


def _generate_feedback_reply(text, rating, product_name, pros, cons, user_name):
    """Generate reply for a feedback.

    Strategy:
    - Good reviews (4-5 stars): Thank for choosing our store
    - Neutral reviews (3 stars): Acknowledge, gently address concerns
    - Bad reviews (1-2 stars): Defend the product card, store is right but polite
    """
    greeting = f"Здравствуйте, {user_name}!" if user_name else "Здравствуйте!"

    if rating and int(rating) >= 4:
        # Positive feedback — благодарность за выбор магазина
        parts = [greeting]
        parts.append("Благодарим вас за выбор нашего магазина и за высокую оценку!")
        if pros:
            parts.append(f"Рады, что вы оценили: {pros.rstrip('.')}.")
        if product_name:
            parts.append(f"Надеемся, что «{product_name}» будет радовать вас долгое время.")
        parts.append("Приятных покупок! Всегда рады видеть вас снова.")
        return " ".join(parts)
    elif rating and int(rating) >= 3:
        # Neutral feedback — мягко
        parts = [greeting]
        parts.append("Спасибо за ваш отзыв и за выбор нашего магазина.")
        if cons:
            parts.append(f"Принимаем к сведению ваше замечание.")
        if product_name:
            parts.append(f"Товар «{product_name}» полностью соответствует описанию и характеристикам, указанным в карточке.")
        parts.append("Надеемся, что в следующий раз ваш опыт будет ещё лучше!")
        return " ".join(parts)
    else:
        # Negative feedback — защита карточки, магазин прав, но вежливо
        parts = [greeting]
        parts.append("Спасибо за обратную связь.")
        if product_name:
            parts.append(f"Товар «{product_name}» полностью соответствует описанию и характеристикам, указанным в карточке товара.")
        if cons:
            parts.append(f"Относительно вашего замечания: рекомендуем внимательно ознакомиться с описанием и характеристиками перед покупкой.")
        elif text:
            parts.append("Рекомендуем внимательно ознакомиться с описанием товара и его характеристиками перед оформлением заказа.")
        parts.append("Вся информация о товаре подробно представлена в карточке.")
        parts.append("Если у вас остались вопросы — будем рады помочь.")
        return " ".join(parts)


def _generate_question_reply(text, product_name, user_name):
    """Generate reply for a question."""
    greeting = f"Здравствуйте, {user_name}!" if user_name else "Здравствуйте!"
    parts = [greeting]
    parts.append("Благодарим за ваш вопрос и интерес к нашему товару.")
    if product_name:
        parts.append(f"Вся актуальная информация о товаре «{product_name}» представлена в описании и характеристиках карточки.")
    parts.append("Если вам нужны дополнительные уточнения — пожалуйста, напишите конкретный вопрос, и мы с радостью ответим.")
    return " ".join(parts)
