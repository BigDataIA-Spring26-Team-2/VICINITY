"""add pipeline infrastructure tables

Revision ID: a132a4da036e
Revises: 5ab9da32649b
Create Date: 2026-04-07 21:43:24.544258
"""
from typing import Sequence, Union
from alembic import op

revision: str = 'a132a4da036e'
down_revision: Union[str, Sequence[str], None] = '5ab9da32649b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DB = "VICINITY_DEV"


def upgrade() -> None:

    op.execute(f"""
        CREATE TABLE IF NOT EXISTS {DB}.RAW.CLASSIFICATION_CACHE (
            source                  VARCHAR(30) NOT NULL,
            field_name              VARCHAR(50) NOT NULL,
            raw_value               VARCHAR(500) NOT NULL,

            severity                VARCHAR(20),
            category                VARCHAR(50),
            narrative               TEXT,

            classified_by           VARCHAR(50),
            classification_version  VARCHAR(10),
            created_at              TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),

            CONSTRAINT pk_class_cache PRIMARY KEY (source, field_name, raw_value)
        )
    """)

    op.execute(f"""
        CREATE TABLE IF NOT EXISTS {DB}.RAW.LLM_USAGE_LOG (
            id                  VARCHAR(36) PRIMARY KEY,
            pipeline_run_id     VARCHAR(36) NOT NULL,
            source              VARCHAR(30) NOT NULL,
            operation           VARCHAR(50) NOT NULL,

            model               VARCHAR(50) NOT NULL,
            input_tokens        INT NOT NULL,
            output_tokens       INT NOT NULL,
            total_tokens        INT NOT NULL,

            input_cost_usd      DECIMAL(10,6),
            output_cost_usd     DECIMAL(10,6),
            total_cost_usd      DECIMAL(10,6),

            batch_size           INT,
            duration_ms          INT,
            created_at           TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
        )
    """)

    op.execute(f"""
        CREATE TABLE IF NOT EXISTS {DB}.RAW.PIPELINE_ERRORS (
            id                  VARCHAR(36) PRIMARY KEY,
            pipeline_run_id     VARCHAR(36) NOT NULL,
            source              VARCHAR(30) NOT NULL,
            record_key          VARCHAR(100),

            error_type          VARCHAR(50) NOT NULL,
            error_message       TEXT,
            raw_record          VARIANT,

            created_at          TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
        )
    """)

    op.execute(f"ALTER TABLE {DB}.RAW.LLM_USAGE_LOG CLUSTER BY (pipeline_run_id)")
    op.execute(f"ALTER TABLE {DB}.RAW.PIPELINE_ERRORS CLUSTER BY (pipeline_run_id, source)")


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS {DB}.RAW.PIPELINE_ERRORS")
    op.execute(f"DROP TABLE IF EXISTS {DB}.RAW.LLM_USAGE_LOG")
    op.execute(f"DROP TABLE IF EXISTS {DB}.RAW.CLASSIFICATION_CACHE")