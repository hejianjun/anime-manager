"""store alphanumeric episode identifiers

Revision ID: 0005_episode_identifier
Revises: 0004_library_scan_path
"""

from alembic import op
import sqlalchemy as sa


revision = "0005_episode_identifier"
down_revision = "0004_library_scan_path"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("media_file") as batch:
        batch.alter_column(
            "episode",
            existing_type=sa.Integer(),
            type_=sa.String(length=16),
            existing_nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("media_file") as batch:
        batch.alter_column(
            "episode",
            existing_type=sa.String(length=16),
            type_=sa.Integer(),
            existing_nullable=True,
        )
