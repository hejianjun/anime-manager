"""add parent task relation

Revision ID: 0006_task_parent
Revises: 0005_episode_identifier
"""

from alembic import op
import sqlalchemy as sa


revision = "0006_task_parent"
down_revision = "0005_episode_identifier"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("task_record")}
    if "parent_task_id" not in columns:
        op.add_column(
            "task_record",
            sa.Column("parent_task_id", sa.Integer(), nullable=True),
        )
    indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("task_record")}
    if "ix_task_record_parent_task_id" not in indexes:
        op.create_index(
            "ix_task_record_parent_task_id",
            "task_record",
            ["parent_task_id"],
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    indexes = {index["name"] for index in inspector.get_indexes("task_record")}
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("task_record")}
    if "parent_task_id" in columns:
        with op.batch_alter_table("task_record", recreate="always") as batch:
            if "ix_task_record_parent_task_id" in indexes:
                batch.drop_index("ix_task_record_parent_task_id")
            batch.drop_column("parent_task_id")
