"""Automatic social publish quarantine tests (no provider calls)."""

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from flask import Flask

from models import ContentFactory, ContentItem, Seller, SocialAccount, User, db
from services.content_auto_publisher import (
    STALE_PUBLISHING_ERROR,
    _auto_publish_for_factory,
    recover_stale_publishing_items,
)
from services.content_publishers.base_publisher import PublishResult


class TestContentAutoPublishHealth:
    def setup_method(self):
        self.app = Flask(__name__)
        self.app.config.update(
            TESTING=True,
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(self.app)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()

        user = User(
            username="social-health",
            email="social-health@test.local",
            is_active=True,
        )
        user.set_password("synthetic-password")
        seller = Seller(user=user, company_name="Social health")
        db.session.add(seller)
        db.session.flush()
        self.account = SocialAccount(
            seller_id=seller.id,
            platform="vk",
            account_name="Synthetic VK",
            account_id="12345",
            is_active=True,
        )
        db.session.add(self.account)
        db.session.flush()
        self.factory = ContentFactory(
            seller_id=seller.id,
            name="VK auto",
            platform="vk",
            auto_publish=True,
            publish_interval_minutes=1,
            default_social_account_id=self.account.id,
            is_active=True,
        )
        db.session.add(self.factory)
        db.session.flush()
        self.first = self._approved_item(seller.id, "First")
        db.session.commit()

    def teardown_method(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def _approved_item(self, seller_id, title):
        item = ContentItem(
            factory_id=self.factory.id,
            seller_id=seller_id,
            platform="vk",
            content_type="promo_post",
            title=title,
            body_text="Synthetic body",
            status="approved",
        )
        db.session.add(item)
        db.session.flush()
        return item

    def test_terminal_failure_blocks_later_automatic_items(self):
        now = datetime(2026, 7, 18, 20, 0, 0)
        publisher = MagicMock()
        publisher.publish.return_value = PublishResult(
            success=False,
            error="Для загрузки фото нужен user_token",
            error_code="vk_user_token_required",
            terminal=True,
        )
        with patch(
            "services.content_publishers.get_publisher",
            return_value=publisher,
        ):
            _auto_publish_for_factory(self.factory, now, db)
            second = self._approved_item(self.factory.seller_id, "Second")
            db.session.commit()
            _auto_publish_for_factory(
                self.factory, now + timedelta(minutes=2), db,
            )

        db.session.refresh(self.account)
        db.session.refresh(self.first)
        db.session.refresh(second)
        assert self.first.status == "failed"
        assert second.status == "approved"
        assert self.account.last_error_code == "vk_user_token_required"
        assert self.account.automatic_publish_blocked_at == now
        assert publisher.publish.call_count == 1

    def test_transient_failure_does_not_quarantine_account(self):
        now = datetime(2026, 7, 18, 21, 0, 0)
        publisher = MagicMock()
        publisher.publish.return_value = PublishResult(
            success=False,
            error="VK timeout",
            error_code="vk_timeout",
        )
        with patch(
            "services.content_publishers.get_publisher",
            return_value=publisher,
        ):
            _auto_publish_for_factory(self.factory, now, db)
            second = self._approved_item(self.factory.seller_id, "Second")
            db.session.commit()
            _auto_publish_for_factory(
                self.factory, now + timedelta(minutes=2), db,
            )

        db.session.refresh(self.account)
        db.session.refresh(second)
        assert second.status == "failed"
        assert self.account.last_error_code == "vk_timeout"
        assert self.account.automatic_publish_blocked_at is None
        assert publisher.publish.call_count == 2

    def test_stale_publish_claim_fails_closed_without_provider_retry(self):
        now = datetime(2026, 7, 18, 22, 0, 0)
        self.first.status = "publishing"
        fresh = self._approved_item(self.factory.seller_id, "Fresh claim")
        fresh.status = "publishing"
        db.session.commit()
        ContentItem.query.filter_by(id=self.first.id).update({
            "updated_at": now - timedelta(minutes=31),
        })
        ContentItem.query.filter_by(id=fresh.id).update({
            "updated_at": now - timedelta(minutes=29),
        })
        db.session.commit()

        recovered = recover_stale_publishing_items(db, now=now)

        db.session.refresh(self.first)
        db.session.refresh(fresh)
        assert recovered == 1
        assert self.first.status == "failed"
        assert self.first.error_message == STALE_PUBLISHING_ERROR
        assert fresh.status == "publishing"
