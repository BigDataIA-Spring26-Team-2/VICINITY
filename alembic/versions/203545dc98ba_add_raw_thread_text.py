"""add raw_thread_text to lifestyle_signals

Revision ID: 203545dc98ba
Revises: d7609ca7fb73
Create Date: 2026-04-13

Adds TEXT column for storing untruncated Reddit thread content
(post + all comments) for downstream Pinecone embedding.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '203545dc98ba'
down_revision: Union[str, Sequence[str], None] = 'd7609ca7fb73'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE RAW.LIFESTYLE_SIGNALS "
        "ADD COLUMN IF NOT EXISTS raw_thread_text TEXT"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE RAW.LIFESTYLE_SIGNALS "
        "DROP COLUMN IF EXISTS raw_thread_text"
    )
