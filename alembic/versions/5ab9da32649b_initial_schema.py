"""initial schema

Revision ID: 5ab9da32649b
Revises:
Create Date: 2026-04-07 16:24:36.200555
"""
from typing import Sequence, Union
from alembic import op

revision: str = '5ab9da32649b'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DB = "VICINITY_DEV"


def upgrade() -> None:

    # ── USER_DATA ────────────────────────────────────────────

    op.execute(f"""
        CREATE TABLE IF NOT EXISTS {DB}.USER_DATA.USERS (
            id              VARCHAR(36) PRIMARY KEY,
            email           VARCHAR(255) NOT NULL UNIQUE,
            display_name    VARCHAR(100),
            created_at      TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
            updated_at      TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
        )
    """)

    op.execute(f"""
        CREATE TABLE IF NOT EXISTS {DB}.USER_DATA.SEARCH_PROFILES (
            id                  VARCHAR(36) PRIMARY KEY,
            user_id             VARCHAR(36) NOT NULL,
            profile_name        VARCHAR(100),

            work_address        VARCHAR(500),
            work_lat            FLOAT,
            work_lon            FLOAT,

            budget_min          INT,
            budget_max          INT,
            bedrooms_min        INT,
            bedrooms_max        INT,
            max_commute_min     INT,

            preferences_text    TEXT,
            preference_tags     ARRAY,

            is_active           BOOLEAN DEFAULT TRUE,
            created_at          TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
            updated_at          TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),

            FOREIGN KEY (user_id) REFERENCES {DB}.USER_DATA.USERS(id)
        )
    """)

    op.execute(f"""
        CREATE TABLE IF NOT EXISTS {DB}.USER_DATA.BOOKMARKED_LISTINGS (
            id              VARCHAR(36) PRIMARY KEY,
            user_id         VARCHAR(36) NOT NULL,
            listing_id      VARCHAR(64) NOT NULL,

            notes           TEXT,
            is_active       BOOLEAN DEFAULT TRUE,
            added_at        TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
            removed_at      TIMESTAMP_NTZ,

            CONSTRAINT uq_bookmark UNIQUE (user_id, listing_id),
            FOREIGN KEY (user_id) REFERENCES {DB}.USER_DATA.USERS(id)
        )
    """)

    op.execute(f"""
        CREATE TABLE IF NOT EXISTS {DB}.USER_DATA.CONFIGURED_ROUTES (
            id                  VARCHAR(36) PRIMARY KEY,
            user_id             VARCHAR(36) NOT NULL,
            listing_id          VARCHAR(64) NOT NULL,

            dest_label          VARCHAR(100) NOT NULL,
            dest_address        VARCHAR(500),
            dest_lat            FLOAT,
            dest_lon            FLOAT,

            departure_hour      INT,
            travel_mode         VARCHAR(20),
            duration_min        FLOAT,
            distance_text       VARCHAR(50),
            transit_lines       ARRAY,
            waypoints           ARRAY,
            waypoint_scores     VARIANT,

            route_source        VARCHAR(30),
            is_active           BOOLEAN DEFAULT TRUE,
            computed_at         TIMESTAMP_NTZ,

            CONSTRAINT uq_route UNIQUE (user_id, listing_id, dest_label),
            FOREIGN KEY (user_id) REFERENCES {DB}.USER_DATA.USERS(id)
        )
    """)

    # ── RAW ──────────────────────────────────────────────────

    op.execute(f"""
        CREATE TABLE IF NOT EXISTS {DB}.RAW.LISTINGS (
            listing_id              VARCHAR(64) PRIMARY KEY,
            source                  VARCHAR(20) NOT NULL,
            source_native_id        VARCHAR(50) NOT NULL,
            source_url              VARCHAR(500),

            price                   INT,
            beds                    INT,
            baths                   INT,
            sqft                    INT,
            street                  VARCHAR(200),
            unit                    VARCHAR(50),
            city                    VARCHAR(100),
            zip_code                VARCHAR(10),
            neighborhood            VARCHAR(100),
            lat                     FLOAT,
            lon                     FLOAT,

            description_text        TEXT,
            primary_photo_url       VARCHAR(500),
            mls_id                  VARCHAR(50),
            mls_status              VARCHAR(30),
            days_on_mls             INT,
            agent_name              VARCHAR(200),
            style                   VARCHAR(50),
            list_date               TIMESTAMP_NTZ,

            is_current              BOOLEAN DEFAULT TRUE,
            first_seen_at           TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
            last_seen_at            TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),

            raw_json                VARIANT,
            classification_metadata VARIANT,
            pipeline_run_id         VARCHAR(36),
            scraped_at              TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
        )
    """)

    op.execute(f"""
        CREATE TABLE IF NOT EXISTS {DB}.RAW.CRIME_INCIDENTS (
            incident_id             VARCHAR(30) PRIMARY KEY,
            offense_code            VARCHAR(10),
            offense_description     VARCHAR(100),
            severity                VARCHAR(20),

            occurred_on_date        TIMESTAMP_NTZ,
            hour                    INT,
            day_of_week             VARCHAR(10),

            district                VARCHAR(5),
            street                  VARCHAR(100),
            lat                     FLOAT,
            lon                     FLOAT,
            shooting                BOOLEAN DEFAULT FALSE,

            classification_metadata VARIANT,
            source_resource_id      VARCHAR(50),
            pipeline_run_id         VARCHAR(36),
            scraped_at              TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
        )
    """)

    op.execute(f"""
        CREATE TABLE IF NOT EXISTS {DB}.RAW.COMPLAINTS_311 (
            case_enquiry_id         VARCHAR(20) PRIMARY KEY,
            source_resource_id      VARCHAR(50) NOT NULL,

            open_dt                 TIMESTAMP_NTZ,
            closed_dt               TIMESTAMP_NTZ,
            case_status             VARCHAR(20),

            case_title              VARCHAR(100),
            subject                 VARCHAR(100),
            reason                  VARCHAR(100),
            type                    VARCHAR(100),
            category                VARCHAR(30),

            neighborhood            VARCHAR(100),
            ward                    VARCHAR(10),
            street                  VARCHAR(200),
            zip_code                VARCHAR(10),
            lat                     FLOAT,
            lon                     FLOAT,

            classification_metadata VARIANT,
            pipeline_run_id         VARCHAR(36),
            scraped_at              TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
        )
    """)

    op.execute(f"""
        CREATE TABLE IF NOT EXISTS {DB}.RAW.CITIZEN_INCIDENTS (
            incident_key            VARCHAR(50) PRIMARY KEY,
            title                   VARCHAR(200),
            description             TEXT,
            categories              ARRAY,

            severity                VARCHAR(20),
            level                   INT,
            is_nighttime            BOOLEAN DEFAULT FALSE,

            lat                     FLOAT,
            lon                     FLOAT,
            address                 VARCHAR(300),
            police_district         VARCHAR(20),

            incident_ts             TIMESTAMP_NTZ,
            source                  VARCHAR(20),
            closed                  BOOLEAN DEFAULT FALSE,

            classification_metadata VARIANT,
            pipeline_run_id         VARCHAR(36),
            scraped_at              TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
        )
    """)

    op.execute(f"""
        CREATE TABLE IF NOT EXISTS {DB}.RAW.TRANSIT_STOPS (
            stop_id                 VARCHAR(20) PRIMARY KEY,
            stop_name               VARCHAR(100) NOT NULL,
            lat                     FLOAT,
            lon                     FLOAT,
            municipality            VARCHAR(50),
            wheelchair_boarding     INT,

            route_ids               ARRAY,
            route_names             ARRAY,
            route_types             ARRAY,

            pipeline_run_id         VARCHAR(36),
            scraped_at              TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
        )
    """)

    op.execute(f"""
        CREATE TABLE IF NOT EXISTS {DB}.RAW.AMENITIES (
            osm_id                  BIGINT PRIMARY KEY,
            name                    VARCHAR(200),
            category                VARCHAR(50) NOT NULL,
            lat                     FLOAT NOT NULL,
            lon                     FLOAT NOT NULL,

            address                 VARCHAR(200),
            opening_hours           VARCHAR(200),
            cuisine                 VARCHAR(100),
            sport                   VARCHAR(50),

            query_center_lat        FLOAT,
            query_center_lon        FLOAT,
            query_radius_m          INT,

            pipeline_run_id         VARCHAR(36),
            scraped_at              TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
        )
    """)

    op.execute(f"""
        CREATE TABLE IF NOT EXISTS {DB}.RAW.LIFESTYLE_SIGNALS (
            signal_id               VARCHAR(64) PRIMARY KEY,
            signal_source           VARCHAR(30) NOT NULL,
            source_native_id        VARCHAR(100),

            preference_tag          VARCHAR(50) NOT NULL,
            title                   VARCHAR(500),
            snippet_text            TEXT,
            url                     VARCHAR(500),
            content_hash            VARCHAR(64),

            sentiment               VARCHAR(20),
            relevance_score         INT,
            lat                     FLOAT,
            lon                     FLOAT,

            classification_metadata VARIANT,
            pipeline_run_id         VARCHAR(36),
            fetched_at              TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
        )
    """)

    # ── SCORECARDS ───────────────────────────────────────────

    op.execute(f"""
        CREATE TABLE IF NOT EXISTS {DB}.SCORECARDS.LOCATION_SCORECARD (
            listing_id              VARCHAR(64) NOT NULL,
            score_date              DATE NOT NULL,

            crime_count_500m_7d     INT,
            violent_count_500m_7d   INT,
            crime_trend             VARCHAR(10),

            complaint_count_500m_7d INT,
            complaint_top_types     VARIANT,

            citizen_incidents_48h   INT,
            citizen_nighttime_48h   INT,

            nearby_transit_stops    INT,
            nearby_amenity_count    INT,

            listing_active          BOOLEAN,
            current_price           INT,
            price_vs_first_seen     INT,

            safety_score            INT,
            livability_score        INT,

            scoring_metadata        VARIANT,
            pipeline_run_id         VARCHAR(36),

            CONSTRAINT pk_loc_scorecard PRIMARY KEY (listing_id, score_date)
        )
    """)

    op.execute(f"""
        CREATE TABLE IF NOT EXISTS {DB}.SCORECARDS.LISTING_SUMMARY (
            listing_id              VARCHAR(64) PRIMARY KEY,

            source                  VARCHAR(20),
            source_url              VARCHAR(500),

            price                   INT,
            beds                    INT,
            baths                   INT,
            sqft                    INT,
            street                  VARCHAR(200),
            city                    VARCHAR(100),
            zip_code                VARCHAR(10),
            neighborhood            VARCHAR(100),
            lat                     FLOAT,
            lon                     FLOAT,
            description_text        TEXT,
            primary_photo_url       VARCHAR(500),
            is_active               BOOLEAN,
            list_date               TIMESTAMP_NTZ,

            safety_score            INT,
            safety_metadata         VARIANT,
            livability_score        INT,
            livability_metadata     VARIANT,

            lifestyle_scores        VARIANT,
            nearby_amenities        VARIANT,
            nearest_stops           VARIANT,

            price_history           VARIANT,
            safety_trend            VARIANT,
            price_vs_first_seen     INT,

            last_scored_at          TIMESTAMP_NTZ,
            score_version           VARCHAR(20),
            pipeline_run_id         VARCHAR(36)
        )
    """)

    # ── Clustering keys ──────────────────────────────────────

    op.execute(f"ALTER TABLE {DB}.RAW.CRIME_INCIDENTS CLUSTER BY (district, occurred_on_date)")
    op.execute(f"ALTER TABLE {DB}.RAW.COMPLAINTS_311 CLUSTER BY (neighborhood, open_dt)")
    op.execute(f"ALTER TABLE {DB}.RAW.LISTINGS CLUSTER BY (city, is_current)")
    op.execute(f"ALTER TABLE {DB}.SCORECARDS.LOCATION_SCORECARD CLUSTER BY (listing_id, score_date)")


def downgrade() -> None:
    tables = [
        "SCORECARDS.LISTING_SUMMARY",
        "SCORECARDS.LOCATION_SCORECARD",
        "RAW.LIFESTYLE_SIGNALS",
        "RAW.AMENITIES",
        "RAW.TRANSIT_STOPS",
        "RAW.CITIZEN_INCIDENTS",
        "RAW.COMPLAINTS_311",
        "RAW.CRIME_INCIDENTS",
        "RAW.LISTINGS",
        "USER_DATA.CONFIGURED_ROUTES",
        "USER_DATA.BOOKMARKED_LISTINGS",
        "USER_DATA.SEARCH_PROFILES",
        "USER_DATA.USERS",
    ]
    for t in tables:
        op.execute(f"DROP TABLE IF EXISTS {DB}.{t}")