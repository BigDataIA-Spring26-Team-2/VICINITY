"""expand_amenities_schema

Revision ID: d7609ca7fb73
Revises: a132a4da036e
Create Date: 2026-04-11 20:04:18.557798

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd7609ca7fb73'
down_revision: Union[str, Sequence[str], None] = 'a132a4da036e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop dead columns
    op.execute("ALTER TABLE RAW.AMENITIES DROP COLUMN IF EXISTS cuisine")
    op.execute("ALTER TABLE RAW.AMENITIES DROP COLUMN IF EXISTS sport")
    op.execute("ALTER TABLE RAW.AMENITIES DROP COLUMN IF EXISTS query_center_lat")
    op.execute("ALTER TABLE RAW.AMENITIES DROP COLUMN IF EXISTS query_center_lon")
    op.execute("ALTER TABLE RAW.AMENITIES DROP COLUMN IF EXISTS query_radius_m")

    # Add new columns
    op.execute("ALTER TABLE RAW.AMENITIES ADD COLUMN IF NOT EXISTS subcategory VARCHAR(50) NOT NULL DEFAULT 'unknown'")
    op.execute("ALTER TABLE RAW.AMENITIES ADD COLUMN IF NOT EXISTS website TEXT")
    op.execute("ALTER TABLE RAW.AMENITIES ADD COLUMN IF NOT EXISTS phone VARCHAR(100)")
    op.execute("ALTER TABLE RAW.AMENITIES ADD COLUMN IF NOT EXISTS brand TEXT")
    op.execute("ALTER TABLE RAW.AMENITIES ADD COLUMN IF NOT EXISTS wheelchair VARCHAR(20)")
    op.execute("ALTER TABLE RAW.AMENITIES ADD COLUMN IF NOT EXISTS tags VARIANT")

    # Widen original columns — initial migration used VARCHAR(200) which
    # truncates OSM user-contributed content (opening_hours, names, etc.)
    op.execute("ALTER TABLE RAW.AMENITIES ALTER COLUMN name SET DATA TYPE TEXT")
    op.execute("ALTER TABLE RAW.AMENITIES ALTER COLUMN address SET DATA TYPE TEXT")
    op.execute("ALTER TABLE RAW.AMENITIES ALTER COLUMN opening_hours SET DATA TYPE TEXT")

def downgrade() -> None:
    # Restore original column widths
    op.execute("ALTER TABLE RAW.AMENITIES ALTER COLUMN name SET DATA TYPE VARCHAR(200)")
    op.execute("ALTER TABLE RAW.AMENITIES ALTER COLUMN address SET DATA TYPE VARCHAR(200)")
    op.execute("ALTER TABLE RAW.AMENITIES ALTER COLUMN opening_hours SET DATA TYPE VARCHAR(200)")

    op.execute("ALTER TABLE RAW.AMENITIES DROP COLUMN IF EXISTS subcategory")
    op.execute("ALTER TABLE RAW.AMENITIES DROP COLUMN IF EXISTS website")
    op.execute("ALTER TABLE RAW.AMENITIES DROP COLUMN IF EXISTS phone")
    op.execute("ALTER TABLE RAW.AMENITIES DROP COLUMN IF EXISTS brand")
    op.execute("ALTER TABLE RAW.AMENITIES DROP COLUMN IF EXISTS wheelchair")
    op.execute("ALTER TABLE RAW.AMENITIES DROP COLUMN IF EXISTS tags")

    op.execute("ALTER TABLE RAW.AMENITIES ADD COLUMN IF NOT EXISTS cuisine VARCHAR(100)")
    op.execute("ALTER TABLE RAW.AMENITIES ADD COLUMN IF NOT EXISTS sport VARCHAR(50)")
    op.execute("ALTER TABLE RAW.AMENITIES ADD COLUMN IF NOT EXISTS query_center_lat FLOAT")
    op.execute("ALTER TABLE RAW.AMENITIES ADD COLUMN IF NOT EXISTS query_center_lon FLOAT")
    op.execute("ALTER TABLE RAW.AMENITIES ADD COLUMN IF NOT EXISTS query_radius_m INT")