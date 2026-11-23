"""Evidence from the exact Ozon dictionary search used for brand preparation."""

from models import db


class OzonBrandSearchEvidence(db.Model):
    __tablename__ = "ozon_brand_search_evidence"

    scope_hash = db.Column(db.CHAR(64), primary_key=True)
    seller_id = db.Column(
        db.Integer, db.ForeignKey("sellers.id", ondelete="CASCADE"), nullable=False,
    )
    account_id = db.Column(
        db.Integer,
        db.ForeignKey("seller_marketplace_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    status = db.Column(db.String(16), nullable=False)
    choices_json = db.Column(db.Text, nullable=False, default="[]", server_default="[]")
    reserved_at = db.Column(db.DateTime, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    finished_at = db.Column(db.DateTime)
    complete_result = db.Column(
        db.Boolean, nullable=False, default=False, server_default=db.false(),
    )

    __table_args__ = (
        db.CheckConstraint("length(scope_hash) = 64", name="ck_ozon_brand_search_scope"),
        db.CheckConstraint(
            "status IN ('reserved', 'completed', 'unavailable')",
            name="ck_ozon_brand_search_status",
        ),
        db.CheckConstraint("expires_at > reserved_at", name="ck_ozon_brand_search_expiry"),
        db.CheckConstraint(
            "(status = 'reserved' AND finished_at IS NULL) OR "
            "(status IN ('completed', 'unavailable') AND finished_at >= reserved_at "
            "AND finished_at IS NOT NULL)",
            name="ck_ozon_brand_search_finished",
        ),
        db.CheckConstraint(
            "complete_result IN (0, 1) AND "
            "(status = 'completed' OR (choices_json = '[]' AND complete_result = 0))",
            name="ck_ozon_brand_search_choices",
        ),
        db.Index("ix_ozon_brand_search_seller", "seller_id"),
        db.Index("ix_ozon_brand_search_account", "account_id"),
    )
