"""Exact-kind facades for the shared durable read worker (no new transport)."""
from services.marketplace_inbox import MarketplaceInboxService, MarketplaceInboxValidationError


class _InboxReadService:
    CACHE_TTL = MarketplaceInboxService.CACHE_TTL
    source_kind = None

    @classmethod
    def _period(cls, period_code, *, today):
        if period_code != '90d':
            raise MarketplaceInboxValidationError('Отзывы и вопросы загружаются за 90 дней')
        return period_code, *MarketplaceInboxService._period(today=today)

    @classmethod
    def _fresh_completed(cls, *, period_code, **kwargs):
        cls._period(period_code, today=kwargs['period_end'])
        return MarketplaceInboxService._fresh_completed(source_kind=cls.source_kind, **kwargs)

    @classmethod
    def sync_account(cls, *, period_code, **kwargs):
        cls._period(period_code, today=kwargs['today'])
        return MarketplaceInboxService.sync_kind(source_kind=cls.source_kind, **kwargs)


class OzonReviewReadService(_InboxReadService):
    source_kind = 'review'


class OzonQuestionReadService(_InboxReadService):
    source_kind = 'question'
