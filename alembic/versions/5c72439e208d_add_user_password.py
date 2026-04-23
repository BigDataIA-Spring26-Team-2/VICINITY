"""add user password

Revision ID: 5c72439e208d
Revises: 140e41fdefce
Create Date: 2026-04-20 18:41:22.083631

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from app.config import get_settings



# revision identifiers, used by Alembic.
revision: str = '5c72439e208d'
down_revision: Union[str, Sequence[str], None] = '140e41fdefce'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    db = get_settings().snowflake_database
    op.execute(
        f"ALTER TABLE {db}.USER_DATA.USERS "
        f"ADD COLUMN IF NOT EXISTS password_hash VARCHAR(100)"
    )
 
 
def downgrade() -> None:
    db = get_settings().snowflake_database
    op.execute(
        f"ALTER TABLE {db}.USER_DATA.USERS "
        f"DROP COLUMN IF EXISTS password_hash"
    )
 