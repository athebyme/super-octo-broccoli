import requests
import time
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

FEEDBACKS_API_URL = "https://feedbacks-api.wildberries.ru"

class FeedbackService:
    """Service for interacting with WB Feedbacks & Questions API."""

    RATE_LIMIT_DELAY = 0.35  # 3 requests per second limit

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': api_key,
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        })
        self._last_request_time = 0

    def _rate_limit(self):
        """Ensure we don't exceed 3 requests per second."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self.RATE_LIMIT_DELAY:
            time.sleep(self.RATE_LIMIT_DELAY - elapsed)
        self._last_request_time = time.time()

    def _get(self, path, params=None):
        """Make GET request with rate limiting."""
        self._rate_limit()
        url = f"{FEEDBACKS_API_URL}{path}"
        resp = self.session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path, json_data=None):
        """Make POST request with rate limiting."""
        self._rate_limit()
        url = f"{FEEDBACKS_API_URL}{path}"
        resp = self.session.post(url, json=json_data, timeout=30)
        if resp.status_code == 204:
            return {'success': True}
        resp.raise_for_status()
        return resp.json()

    def _patch(self, path, json_data=None):
        """Make PATCH request with rate limiting."""
        self._rate_limit()
        url = f"{FEEDBACKS_API_URL}{path}"
        resp = self.session.patch(url, json=json_data, timeout=30)
        resp.raise_for_status()
        return resp.json()

    # --- Feedbacks ---

    def get_feedbacks(self, is_answered=True, take=100, skip=0, nm_id=None, order='dateDesc', date_from=None, date_to=None):
        """Get list of feedbacks."""
        params = {
            'isAnswered': str(is_answered).lower(),
            'take': min(take, 5000),
            'skip': skip,
            'order': order,
        }
        if nm_id:
            params['nmId'] = nm_id
        if date_from:
            params['dateFrom'] = int(date_from)
        if date_to:
            params['dateTo'] = int(date_to)
        return self._get('/api/v1/feedbacks', params)

    def get_feedbacks_unanswered_count(self):
        """Get count of unanswered feedbacks + average rating."""
        return self._get('/api/v1/feedbacks/count-unanswered')

    def get_feedbacks_count(self, is_answered=True, date_from=None, date_to=None):
        """Get count of feedbacks by period."""
        params = {'isAnswered': str(is_answered).lower()}
        if date_from:
            params['dateFrom'] = int(date_from)
        if date_to:
            params['dateTo'] = int(date_to)
        return self._get('/api/v1/feedbacks/count', params)

    def answer_feedback(self, feedback_id: str, text: str):
        """Answer a feedback. Text must be 2-5000 characters."""
        if len(text) < 2 or len(text) > 5000:
            raise ValueError("Answer text must be 2-5000 characters")
        return self._post('/api/v1/feedbacks/answer', {
            'id': feedback_id,
            'text': text
        })

    # --- Questions ---

    def get_questions(self, is_answered=True, take=100, skip=0, nm_id=None, order='dateDesc', date_from=None, date_to=None):
        """Get list of questions."""
        params = {
            'isAnswered': str(is_answered).lower(),
            'take': min(take, 10000),
            'skip': skip,
            'order': order,
        }
        if nm_id:
            params['nmId'] = nm_id
        if date_from:
            params['dateFrom'] = int(date_from)
        if date_to:
            params['dateTo'] = int(date_to)
        return self._get('/api/v1/questions', params)

    def get_questions_unanswered_count(self):
        """Get count of unanswered questions."""
        return self._get('/api/v1/questions/count-unanswered')

    def answer_question(self, question_id: str, text: str):
        """Answer a question."""
        return self._patch('/api/v1/questions', {
            'id': question_id,
            'answer': {'text': text},
            'state': 'wbRu'
        })

    # --- Combined ---

    def check_new_items(self):
        """Check if there are new unviewed feedbacks or questions."""
        return self._get('/api/v1/new-feedbacks-questions')

    def get_all_unanswered_feedbacks(self, batch_size=500):
        """Load all unanswered feedbacks in batches."""
        all_feedbacks = []
        skip = 0
        while True:
            result = self.get_feedbacks(is_answered=False, take=batch_size, skip=skip)
            data = result.get('data', {})
            feedbacks = data.get('feedbacks', [])
            if not feedbacks:
                break
            all_feedbacks.extend(feedbacks)
            skip += len(feedbacks)
            if skip >= data.get('countUnanswered', 0):
                break
        return all_feedbacks

    def get_all_unanswered_questions(self, batch_size=500):
        """Load all unanswered questions in batches."""
        all_questions = []
        skip = 0
        while True:
            result = self.get_questions(is_answered=False, take=batch_size, skip=skip)
            data = result.get('data', {})
            questions = data.get('questions', [])
            if not questions:
                break
            all_questions.extend(questions)
            skip += len(questions)
            if skip >= data.get('countUnanswered', 0):
                break
        return all_questions

    def get_reputation_stats(self):
        """Get combined reputation statistics."""
        fb_unanswered = self.get_feedbacks_unanswered_count()
        q_unanswered = self.get_questions_unanswered_count()
        fb_total = self.get_feedbacks_count(is_answered=True)
        fb_total_unanswered = self.get_feedbacks_count(is_answered=False)

        return {
            'avg_rating': float(fb_unanswered.get('data', {}).get('valuation', 0) or 0),
            'feedbacks_unanswered': fb_unanswered.get('data', {}).get('countUnanswered', 0),
            'feedbacks_unanswered_today': fb_unanswered.get('data', {}).get('countUnansweredToday', 0),
            'feedbacks_total': (fb_total.get('data', 0) or 0) + (fb_total_unanswered.get('data', 0) or 0),
            'feedbacks_answered': fb_total.get('data', 0) or 0,
            'questions_unanswered': q_unanswered.get('data', {}).get('countUnanswered', 0),
            'questions_unanswered_today': q_unanswered.get('data', {}).get('countUnansweredToday', 0),
        }

    def get_rating_distribution(self, total_count=1000):
        """Get rating distribution by loading recent feedbacks."""
        distribution = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
        skip = 0
        batch_size = 500
        loaded = 0

        while loaded < total_count:
            result = self.get_feedbacks(is_answered=True, take=batch_size, skip=skip)
            feedbacks = result.get('data', {}).get('feedbacks', [])
            if not feedbacks:
                break
            for fb in feedbacks:
                rating = fb.get('productValuation', 0)
                if rating in distribution:
                    distribution[rating] += 1
            loaded += len(feedbacks)
            skip += len(feedbacks)

        # Also load unanswered ones
        skip = 0
        while loaded < total_count:
            result = self.get_feedbacks(is_answered=False, take=batch_size, skip=skip)
            feedbacks = result.get('data', {}).get('feedbacks', [])
            if not feedbacks:
                break
            for fb in feedbacks:
                rating = fb.get('productValuation', 0)
                if rating in distribution:
                    distribution[rating] += 1
            loaded += len(feedbacks)
            skip += len(feedbacks)

        return distribution
