"""add optional library scan directory

Revision ID: 0004_library_scan_path
Revises: 0003_catalog_health
"""

from alembic import op
import sqlalchemy as sa


revision = "0004_library_scan_path"
down_revision = "0003_catalog_health"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("library_root")
    }
    if "scan_path" not in columns or "scan_last_scan_at" not in columns:
        with op.batch_alter_table("library_root") as batch:
            if "scan_path" not in columns:
                batch.add_column(
                    sa.Column("scan_path", sa.String(length=2048), nullable=True)
                )
            if "scan_last_scan_at" not in columns:
                batch.add_column(
                    sa.Column(
                        "scan_last_scan_at",
                        sa.DateTime(timezone=True),
                        nullable=True,
                    )
                )


def downgrade() -> None:
    with op.batch_alter_table("library_root") as batch:
        batch.drop_column("scan_last_scan_at")
        batch.drop_column("scan_path")
