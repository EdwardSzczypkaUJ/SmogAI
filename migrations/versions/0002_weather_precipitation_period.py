"""Store precipitation accumulation semantics explicitly.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-03
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = {
        column["name"]
        for column in inspector.get_columns("weather_measurements")
    }
    if "precipitation_accumulation_period_hours" in existing:
        return
    with op.batch_alter_table("weather_measurements") as batch:
        batch.add_column(
            sa.Column("precipitation_accumulation_period_hours", sa.Integer(), nullable=True)
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = {
        column["name"]
        for column in inspector.get_columns("weather_measurements")
    }
    if "precipitation_accumulation_period_hours" not in existing:
        return
    with op.batch_alter_table("weather_measurements") as batch:
        batch.drop_column("precipitation_accumulation_period_hours")
