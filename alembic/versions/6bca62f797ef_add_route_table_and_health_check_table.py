"""add_route_table and health check table

Revision ID: 6bca62f797ef
Revises: 3add0f393d54
Create Date: 2026-04-15 01:16:20.742733

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6bca62f797ef'
down_revision: Union[str, Sequence[str], None] = '3add0f393d54'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:

    # ── HEALTHZ ──────────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS RAW.HEALTHZ (
            id              VARCHAR(36) PRIMARY KEY,
            checked_at      TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
            status          VARCHAR(20) DEFAULT 'ok',
            client_ip       VARCHAR(45),
            user_agent      VARCHAR(500),
            response_ms     INT,
            details         VARIANT
        )
    """)

    # ── ROUTE_SCORECARD ──────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS SCORECARDS.ROUTE_SCORECARD (
            route_id            VARCHAR(36) NOT NULL,
            listing_id          VARCHAR(64) NOT NULL,
            score_date          DATE NOT NULL,

            crime_count         INT,
            violent_count       INT,
            shooting_count      INT,
            crimes_at_dep_hour  INT,

            citizen_incidents   INT,
            citizen_nighttime   INT,

            scoring_metadata    VARIANT,
            pipeline_run_id     VARCHAR(36),

            CONSTRAINT pk_route_scorecard PRIMARY KEY (route_id, score_date)
        )
    """)

    op.execute(
        "ALTER TABLE SCORECARDS.ROUTE_SCORECARD "
        "CLUSTER BY (route_id, score_date)"
    )

    # ── LOCATION_SCORECARD: rename stale columns ─────────────
    op.execute(
        "ALTER TABLE SCORECARDS.LOCATION_SCORECARD "
        "RENAME COLUMN crime_count_500m_7d TO crime_count"
    )
    op.execute(
        "ALTER TABLE SCORECARDS.LOCATION_SCORECARD "
        "RENAME COLUMN violent_count_500m_7d TO violent_count"
    )
    op.execute(
        "ALTER TABLE SCORECARDS.LOCATION_SCORECARD "
        "RENAME COLUMN complaint_count_500m_7d TO complaint_count"
    )

    # Drop unused — pipeline writes these into scoring_metadata
    op.execute(
        "ALTER TABLE SCORECARDS.LOCATION_SCORECARD "
        "DROP COLUMN IF EXISTS complaint_top_types"
    )
    op.execute(
        "ALTER TABLE SCORECARDS.LOCATION_SCORECARD "
        "DROP COLUMN IF EXISTS price_vs_first_seen"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE SCORECARDS.LOCATION_SCORECARD "
        "RENAME COLUMN crime_count TO crime_count_500m_7d"
    )
    op.execute(
        "ALTER TABLE SCORECARDS.LOCATION_SCORECARD "
        "RENAME COLUMN violent_count TO violent_count_500m_7d"
    )
    op.execute(
        "ALTER TABLE SCORECARDS.LOCATION_SCORECARD "
        "RENAME COLUMN complaint_count TO complaint_count_500m_7d"
    )
    op.execute(
        "ALTER TABLE SCORECARDS.LOCATION_SCORECARD "
        "ADD COLUMN IF NOT EXISTS complaint_top_types VARIANT"
    )
    op.execute(
        "ALTER TABLE SCORECARDS.LOCATION_SCORECARD "
        "ADD COLUMN IF NOT EXISTS price_vs_first_seen INT"
    )

    op.execute("DROP TABLE IF EXISTS SCORECARDS.ROUTE_SCORECARD")
    op.execute("DROP TABLE IF EXISTS RAW.HEALTHZ")