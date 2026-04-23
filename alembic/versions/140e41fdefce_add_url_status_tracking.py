"""add url status tracking

Revision ID: 140e41fdefce
Revises: 6c4024eb849e
Create Date: 2026-04-18 20:09:18.435711

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from app.config import get_settings



# revision identifiers, used by Alembic.
revision: str = '140e41fdefce'
down_revision: Union[str, Sequence[str], None] = '6c4024eb849e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    db = get_settings().snowflake_database
 
    for table in ("RAW.LISTINGS", "RAW.LIFESTYLE_SIGNALS"):
        op.execute(
            f"ALTER TABLE {db}.{table} "
            f"ADD COLUMN IF NOT EXISTS url_status VARCHAR(20) DEFAULT 'active'"
        )
        op.execute(
            f"ALTER TABLE {db}.{table} "
            f"ADD COLUMN IF NOT EXISTS url_flagged_at TIMESTAMP_NTZ"
        )
 
 
def downgrade() -> None:
    db = get_settings().snowflake_database
 
    for table in ("RAW.LISTINGS", "RAW.LIFESTYLE_SIGNALS"):
        op.execute(
            f"ALTER TABLE {db}.{table} DROP COLUMN IF EXISTS url_flagged_at"
        )
        op.execute(
            f"ALTER TABLE {db}.{table} DROP COLUMN IF EXISTS url_status"
        )