"""create x_ui_migrations table

Revision ID: vlt_0004
Revises: vlt_0003
Create Date: 2026-04-21

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'vlt_0004'
down_revision: Union[str, None] = 'vlt_0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'x_ui_migrations',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('old_uuid', sa.String(64), nullable=False, unique=True),
        sa.Column(
            'user_id',
            sa.Integer(),
            sa.ForeignKey('users.id', ondelete='CASCADE'),
            nullable=False,
            index=True,
        ),
        sa.Column(
            'subscription_id',
            sa.Integer(),
            sa.ForeignKey('subscriptions.id', ondelete='SET NULL'),
            nullable=True,
            index=True,
        ),
        sa.Column('tariff_id', sa.Integer(), nullable=True),
        sa.Column('old_email', sa.String(255), nullable=True),
        sa.Column('source_db', sa.String(255), nullable=True),
        sa.Column('old_expiry_time_ms', sa.BigInteger(), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table('x_ui_migrations')
