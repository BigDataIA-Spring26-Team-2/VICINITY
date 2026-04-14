"""adding embedding tracking table

Revision ID: 3add0f393d54
Revises: 203545dc98ba
Create Date: 2026-04-14 01:51:21.872595

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from app.config import get_settings



# revision identifiers, used by Alembic.
revision: str = '3add0f393d54'
down_revision: Union[str, Sequence[str], None] = '203545dc98ba'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    db = get_settings().snowflake_database

    op.execute(f"""
        CREATE TABLE IF NOT EXISTS {db}.RAW.EMBEDDING_SYNC (
            signal_id       VARCHAR(64) PRIMARY KEY,
            content_hash    VARCHAR(64) NOT NULL,
            embedding_model VARCHAR(50) NOT NULL,
            vector_dim      INT NOT NULL,
            embedded_at     TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
        )
    """)
    op.execute(
        f"ALTER TABLE {db}.RAW.EMBEDDING_SYNC CLUSTER BY (signal_id)"
    )

def downgrade() -> None:
    """Downgrade schema."""
    db = get_settings().snowflake_database
    op.execute(f"DROP TABLE IF EXISTS {db}.RAW.EMBEDDING_SYNC")