"""store catalog sidecar health

Revision ID: 0003_catalog_health
Revises: 0002
"""

from alembic import op
import sqlalchemy as sa


revision = "0003_catalog_health"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("anime") as batch:
        batch.add_column(
            sa.Column(
                "has_show_nfo",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
    with op.batch_alter_table("media_file") as batch:
        batch.add_column(
            sa.Column(
                "has_nfo",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.add_column(
            sa.Column(
                "has_episode_image",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("media_file") as batch:
        batch.drop_column("has_episode_image")
        batch.drop_column("has_nfo")
    with op.batch_alter_table("anime") as batch:
        batch.drop_column("has_show_nfo")
