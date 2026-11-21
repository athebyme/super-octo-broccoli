"""Storage for explicit administrator requests to refresh a supplier catalog."""

from models import db


class SupplierCatalogRefreshRun(db.Model):
    __tablename__ = "supplier_catalog_refresh_runs"

    id = db.Column(db.String(36), primary_key=True)
    supplier_id = db.Column(
        db.Integer, db.ForeignKey("suppliers.id", ondelete="CASCADE"), nullable=False,
    )
    admin_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    status = db.Column(db.String(24), nullable=False)
    config_digest = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False)
    started_at = db.Column(db.DateTime)
    raw_committed_at = db.Column(db.DateTime)
    completed_at = db.Column(db.DateTime)
    total = db.Column(db.Integer)
    added = db.Column(db.Integer)
    updated = db.Column(db.Integer)
    postprocess_errors = db.Column(db.Integer)
    price_status = db.Column(db.String(24))
    price_conflicts = db.Column(db.Integer)
    error_code = db.Column(db.String(80))

    __table_args__ = (
        db.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'partial', 'failed', 'interrupted')",
            name="ck_supplier_catalog_refresh_status",
        ),
        db.Index("ix_supplier_catalog_refresh_latest", "supplier_id", "created_at", "id"),
        db.Index("ix_supplier_catalog_refresh_queue", "status", "created_at", "id"),
        db.Index(
            "uq_supplier_catalog_refresh_active",
            "supplier_id",
            unique=True,
            sqlite_where=db.text("status IN ('pending', 'running')"),
            postgresql_where=db.text("status IN ('pending', 'running')"),
        ),
    )
