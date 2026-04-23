"""add wactch end and agent conversations table

Revision ID: 6c4024eb849e
Revises: 6bca62f797ef
Create Date: 2026-04-18 18:40:27.077474

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from app.config import get_settings



# revision identifiers, used by Alembic.
revision: str = '6c4024eb849e'
down_revision: Union[str, Sequence[str], None] = '6bca62f797ef'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    db = get_settings().snowflake_database
 
    # ── Watch period on bookmarks ────────────────────────────
    op.execute(
        f"ALTER TABLE {db}.USER_DATA.BOOKMARKED_LISTINGS "
        f"ADD COLUMN IF NOT EXISTS watch_end TIMESTAMP_NTZ"
    )
 
    # ── Raw conversation log ─────────────────────────────────
    op.execute(f"""
        CREATE TABLE IF NOT EXISTS {db}.USER_DATA.CONVERSATIONS (
            id              VARCHAR(36) PRIMARY KEY,
            user_id         VARCHAR(36) NOT NULL,
            session_id      VARCHAR(36) NOT NULL,
            role            VARCHAR(20) NOT NULL,
            content         TEXT NOT NULL,
            tool_calls      VARIANT,
            created_at      TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
 
            FOREIGN KEY (user_id) REFERENCES {db}.USER_DATA.USERS(id)
        )
    """)
 
    op.execute(
        f"ALTER TABLE {db}.USER_DATA.CONVERSATIONS "
        f"CLUSTER BY (user_id, session_id, created_at)"
    )
 
    # ── Session summaries ────────────────────────────────────
    op.execute(f"""
        CREATE TABLE IF NOT EXISTS {db}.USER_DATA.SESSION_SUMMARIES (
            id              VARCHAR(36) PRIMARY KEY,
            user_id         VARCHAR(36) NOT NULL,
            session_id      VARCHAR(36) NOT NULL UNIQUE,
            summary         TEXT NOT NULL,
            decisions       VARIANT,
            pending_actions VARIANT,
            listings_discussed ARRAY,
            message_count   INT,
            created_at      TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
 
            FOREIGN KEY (user_id) REFERENCES {db}.USER_DATA.USERS(id)
        )
    """)
 
    op.execute(
        f"ALTER TABLE {db}.USER_DATA.SESSION_SUMMARIES "
        f"CLUSTER BY (user_id, created_at)"
    )
 
 
def downgrade() -> None:
    db = get_settings().snowflake_database
 
    op.execute(f"DROP TABLE IF EXISTS {db}.USER_DATA.SESSION_SUMMARIES")
    op.execute(f"DROP TABLE IF EXISTS {db}.USER_DATA.CONVERSATIONS")
    op.execute(
        f"ALTER TABLE {db}.USER_DATA.BOOKMARKED_LISTINGS "
        f"DROP COLUMN IF EXISTS watch_end"
    )
 