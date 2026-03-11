from flask import render_template, jsonify, request, flash, redirect, url_for
from flask_login import login_required, current_user
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
        """Generate AI reply for a feedback or question."""
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'API ключ WB не настроен'}), 403
        try:
            body = request.get_json(silent=True) or {}
            item_type = body.get('type', 'feedback')  # feedback or question
            text = body.get('text', '')
            product_name = body.get('productName', '')
            rating = body.get('rating', None)
            pros = body.get('pros', '')
            cons = body.get('cons', '')
            user_name = body.get('userName', '')

            if not text and not pros and not cons:
                return jsonify({'error': 'Текст отзыва/вопроса не указан'}), 400

            # Build AI prompt
            if item_type == 'feedback':
                reply = _generate_feedback_reply(text, rating, product_name, pros, cons, user_name)
            else:
                reply = _generate_question_reply(text, product_name, user_name)

            return jsonify({'reply': reply})
        except Exception as e:
            logger.error(f"Error generating reply: {e}\n{traceback.format_exc()}")
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


def _generate_feedback_reply(text, rating, product_name, pros, cons, user_name):
    """Generate AI reply for a feedback using simple templates.

    For now uses template-based generation. Can be enhanced with AI later.
    """
    greeting = f"Здравствуйте, {user_name}!" if user_name else "Здравствуйте!"

    if rating and int(rating) >= 4:
        # Positive feedback
        parts = [greeting]
        parts.append("Большое спасибо за ваш отзыв и высокую оценку!")
        if pros:
            parts.append(f"Рады, что вам понравилось: {pros.lower().rstrip('.')}.")
        if product_name:
            parts.append(f"Надеемся, что {product_name} будет радовать вас долгое время.")
        parts.append("Будем рады видеть вас снова!")
        return " ".join(parts)
    elif rating and int(rating) >= 3:
        # Neutral feedback
        parts = [greeting]
        parts.append("Благодарим за ваш отзыв.")
        if cons:
            parts.append(f"Мы учтём ваши замечания: {cons.lower().rstrip('.')}.")
        parts.append("Мы постоянно работаем над улучшением качества нашей продукции и сервиса.")
        parts.append("Надеемся, что в следующий раз ваш опыт будет ещё лучше!")
        return " ".join(parts)
    else:
        # Negative feedback
        parts = [greeting]
        parts.append("Благодарим за обратную связь и приносим извинения за доставленные неудобства.")
        if cons:
            parts.append(f"Мы внимательно изучим указанную проблему: {cons.lower().rstrip('.')}.")
        if text:
            parts.append("Ваш отзыв очень важен для нас и поможет улучшить качество продукции.")
        parts.append("Если у вас остались вопросы, пожалуйста, свяжитесь с нами.")
        return " ".join(parts)


def _generate_question_reply(text, product_name, user_name):
    """Generate AI reply for a question using templates."""
    greeting = f"Здравствуйте, {user_name}!" if user_name else "Здравствуйте!"
    parts = [greeting]
    parts.append("Благодарим за ваш вопрос.")
    if product_name:
        parts.append(f"По товару «{product_name}»:")
    parts.append("К сожалению, мы не можем автоматически ответить на данный вопрос.")
    parts.append("Пожалуйста, уточните ваш вопрос, и мы постараемся помочь.")
    return " ".join(parts)
