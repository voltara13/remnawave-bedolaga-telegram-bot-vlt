"""merge subscription and payment migration heads

Revision ID: 0064
Revises: 0060, 0063
Create Date: 2026-04-19

"""

from typing import Sequence, Union


revision: str = '0064'
down_revision: Union[str, Sequence[str], None] = ('0060', '0063')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
