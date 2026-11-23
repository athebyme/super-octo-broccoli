"""Persistent opt-in schedules for full supplier catalog refreshes."""

from models import db

# Register the legacy queue table in the shared metadata so SQLAlchemy can
# resolve the scheduled-run foreign key. Its schema lives in its own module
# and remains owned by the refresh-run migration.
from services.supplier_catalog_refresh_models import SupplierCatalogRefreshRun  # noqa: F401


class SupplierCatalogSchedule(db.Model):
    __tablename__ = "supplier_catalog_schedules"

    supplier_id = db.Column(
        db.Integer,
        db.ForeignKey("suppliers.id", ondelete="CASCADE"),
        primary_key=True,
    )
    enabled = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    interval_hours = db.Column(
        db.Integer, nullable=False, default=24, server_default=db.text("24"),
    )
    next_due_at = db.Column(db.DateTime)
    authorized_by_user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=False,
    )
    revision = db.Column(
        db.Integer, nullable=False, default=1, server_default=db.text("1"),
    )
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        server_default=db.func.current_timestamp(),
        onupdate=db.func.current_timestamp(),
    )

    __table_args__ = (
        db.CheckConstraint(
            "enabled IN (0, 1)",
            name="ck_supplier_catalog_schedule_enabled",
        ),
        db.CheckConstraint(
            "interval_hours BETWEEN 6 AND 168",
            name="ck_supplier_catalog_schedule_interval_hours",
        ),
        db.CheckConstraint(
            "revision > 0",
            name="ck_supplier_catalog_schedule_revision_positive",
        ),
        db.Index(
            "ix_supplier_catalog_schedules_enabled_due",
            "enabled",
            "next_due_at",
        ),
    )


class SupplierCatalogScheduledRun(db.Model):
    __tablename__ = "supplier_catalog_scheduled_runs"

    run_id = db.Column(
        db.String(36),
        db.ForeignKey("supplier_catalog_refresh_runs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    schedule_revision = db.Column(db.Integer, nullable=False)
    due_at = db.Column(db.DateTime, nullable=False)

    __table_args__ = (
        db.CheckConstraint(
            "schedule_revision > 0",
            name="ck_supplier_catalog_scheduled_run_revision_positive",
        ),
    )
