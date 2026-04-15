"""Scoring pipeline — nightly batch scoring for all active listings.

Computes safety, livability, transit percentiles across all active
listings. Stores daily snapshot in LOCATION_SCORECARD. Updates
LISTING_SUMMARY with latest scores.

Usage:
    python -m app.scoring.pipeline --dry-run
    python -m app.scoring.pipeline --mode full
    python -m app.scoring.pipeline --mode incremental
"""

import json
import argparse
from datetime import datetime, timezone

import structlog

from app.core.base_pipeline import BasePipeline, PipelineRunResult
from app.core.config_loader import load_spatial
from app.scoring.config import load_scoring_config
from app.scoring.queries import (
    query_safety,
    query_livability,
    query_transit,
    query_monthly_crime_series,
    query_hourly_distribution,
    query_dow_distribution,
    query_lifestyle_signals_by_neighborhood,
)
from app.scoring.scorer import (
    percentile_rank,
    compute_livability_percentiles,
    compute_safety_confidence,
    compute_livability_confidence,
    compute_transit_confidence,
    compute_yoy_change,
    weighted_composite,
)
from app.scoring.metadata import build_scoring_metadata

logger = structlog.get_logger()


class ScoringPipeline(BasePipeline):
    """Nightly batch scoring for all active listings."""

    SOURCE = "scoring"
    DESCRIPTION = "Compute location scores for all active listings."

    def run_pipeline(self, args: argparse.Namespace) -> PipelineRunResult:
        cfg = load_scoring_config()
        spatial = load_spatial()
        zip_to_hood = spatial["zip_to_neighborhood"]

        self.log.info(
            "scoring_config",
            safety_radius=cfg.safety_radius_m,
            livability_radius=cfg.livability_radius_m,
            transit_radius=cfg.transit_radius_m,
            crime_window=cfg.crime_window_days,
            complaint_window=cfg.complaint_window_days,
            qol_weight=cfg.complaint_qol_weight,
            infra_weight=cfg.complaint_infra_weight,
        )

        # ── Query all dimensions ──────────────────────────────
        safety = query_safety(self.cursor, cfg)
        livability = query_livability(self.cursor, cfg)
        transit = query_transit(self.cursor, cfg)

        total_listings = len(safety)
        if total_listings == 0:
            self.log.info("no_active_listings")
            return PipelineRunResult(
                pipeline_run_id=self.pipeline_run_id,
                source=self.SOURCE,
            )

        self.log.info("dimensions_queried", listings=total_listings)

        # ── Historical series (conditional on config) ─────────
        monthly_series = {}
        hourly_dist = {}
        dow_dist = {}

        if cfg.store_monthly_series:
            monthly_series = query_monthly_crime_series(self.cursor, cfg)
        if cfg.store_hourly_distribution:
            hourly_dist = query_hourly_distribution(self.cursor, cfg)
        if cfg.store_dow_distribution:
            dow_dist = query_dow_distribution(self.cursor, cfg)

        # ── Lifestyle signal overlay ──────────────────────────
        lifestyle_by_hood = query_lifestyle_signals_by_neighborhood(self.cursor)

        # ── Percentile rankings ───────────────────────────────
        safety_raw = {lid: d["crime_count"] for lid, d in safety.items()}
        safety_pct = percentile_rank(safety_raw, lower_better=True)

        liv_pct = compute_livability_percentiles(
            livability, cfg.complaint_qol_weight, cfg.complaint_infra_weight,
        )

        transit_raw = {lid: d["stop_count"] for lid, d in transit.items()}
        transit_pct = percentile_rank(transit_raw, lower_better=False)

        self.log.info("percentiles_computed")

        # ── Per-listing scoring ───────────────────────────────
        records = []

        for lid in safety.keys():
            s = safety.get(lid, {})
            lv = livability.get(lid, {})
            t = transit.get(lid, {})

            sp = safety_pct.get(lid, 0)
            lp = liv_pct.get(lid, 0)
            tp = transit_pct.get(lid, 0)

            # Confidence
            listing_series = monthly_series.get(lid, {})
            s_conf = compute_safety_confidence(
                s.get("crime_count", 0), len(listing_series),
            )
            l_conf = compute_livability_confidence(
                lv.get("complaint_count", 0),
            )
            t_conf = compute_transit_confidence()

            # YoY
            yoy = compute_yoy_change(listing_series) if listing_series else None

            # Composite
            dim_pcts = {"safety": sp, "livability": lp, "transit": tp}
            comp, renorm_weights = weighted_composite(
                dim_pcts, cfg.composite_defaults, batch_only=True,
            )

            # Metadata
            meta = build_scoring_metadata(
                listing_id=lid,
                cfg=cfg,
                safety=s,
                livability=lv,
                transit=t,
                safety_percentile=sp,
                livability_percentile=lp,
                transit_percentile=tp,
                safety_confidence=s_conf,
                livability_confidence=l_conf,
                transit_confidence=t_conf,
                composite=comp,
                renormalized_weights=renorm_weights,
                total_listings=total_listings,
                zip_to_neighborhood=zip_to_hood,
                lifestyle_by_hood=lifestyle_by_hood,
                yoy_change=yoy,
                monthly_series=listing_series if cfg.store_monthly_series else None,
                hourly_distribution=hourly_dist.get(lid) if cfg.store_hourly_distribution else None,
                dow_distribution=dow_dist.get(lid) if cfg.store_dow_distribution else None,
            )

            records.append({
                "listing_id": lid,
                "score_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "crime_count": s.get("crime_count", 0),
                "violent_count": s.get("violent_count", 0),
                "crime_trend": (
                    "declining" if yoy and yoy < -10
                    else "rising" if yoy and yoy > 10
                    else "stable"
                ),
                "complaint_count": lv.get("complaint_count", 0),
                "citizen_incidents": s.get("citizen_total", 0),
                "citizen_nighttime": s.get("citizen_nighttime", 0),
                "transit_stops": t.get("stop_count", 0),
                "amenity_count": lv.get("total_amenities", 0),
                "listing_active": True,
                "current_price": s.get("price"),
                "safety_score": sp,
                "livability_score": lp,
                "scoring_metadata": json.dumps(meta),
                "pipeline_run_id": self.pipeline_run_id,
            })

        self.log.info("scoring_complete", records=len(records))

        if args.dry_run:
            for r in records[:3]:
                m = json.loads(r["scoring_metadata"])
                self.log.info(
                    "sample",
                    listing=r["listing_id"][:16],
                    safety=m["safety"]["percentile"],
                    safety_conf=m["safety"]["confidence"],
                    livability=m["livability"]["percentile"],
                    transit=m["transit"]["percentile"],
                    composite=m["composite"]["score"],
                    effective_complaints=m["livability"]["effective_complaint_score"],
                    has_lifestyle=bool(m.get("lifestyle_overlay")),
                )
            return PipelineRunResult(
                pipeline_run_id=self.pipeline_run_id,
                source=self.SOURCE,
                records_extracted=total_listings,
                records_loaded=0,
            )

        # ── Stage + Merge ─────────────────────────────────────
        stage_table = self._create_staging_table()
        self._stage_batch(stage_table, records)
        loaded = self._merge_scorecard(stage_table)
        self._drop_staging(stage_table)

        # ── Update LISTING_SUMMARY ────────────────────────────
        self._update_listing_summary()

        return PipelineRunResult(
            pipeline_run_id=self.pipeline_run_id,
            source=self.SOURCE,
            records_extracted=total_listings,
            records_loaded=loaded,
        )

    # ── Staging ──────────────────────────────────────────────

    def _create_staging_table(self) -> str:
        batch_id = self.pipeline_run_id[:8]
        table = f"SCORECARDS.SCORE_STAGING_{batch_id}"

        self.cursor.execute(f"""
            CREATE TEMPORARY TABLE {table} (
                listing_id          VARCHAR(64),
                score_date          DATE,
                crime_count         INT,
                violent_count       INT,
                crime_trend         VARCHAR(10),
                complaint_count     INT,
                citizen_incidents   INT,
                citizen_nighttime   INT,
                transit_stops       INT,
                amenity_count       INT,
                listing_active      BOOLEAN,
                current_price       INT,
                safety_score        INT,
                livability_score    INT,
                scoring_metadata    VARCHAR,
                pipeline_run_id     VARCHAR(36)
            )
        """)
        self.log.info("staging_created", table=table)
        return table

    def _stage_batch(self, stage_table: str, records: list[dict]):
        def _safe(v):
            if v is None or type(v) in (str, int, float, bool):
                return v
            return str(v)

        sql = f"""
            INSERT INTO {stage_table} (
                listing_id, score_date, crime_count, violent_count,
                crime_trend, complaint_count, citizen_incidents,
                citizen_nighttime, transit_stops, amenity_count,
                listing_active, current_price, safety_score,
                livability_score, scoring_metadata, pipeline_run_id
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s
            )
        """
        rows = [
            tuple(_safe(v) for v in (
                r["listing_id"], r["score_date"],
                r["crime_count"], r["violent_count"],
                r["crime_trend"], r["complaint_count"],
                r["citizen_incidents"], r["citizen_nighttime"],
                r["transit_stops"], r["amenity_count"],
                r["listing_active"], r["current_price"],
                r["safety_score"], r["livability_score"],
                r["scoring_metadata"], r["pipeline_run_id"],
            ))
            for r in records
        ]
        self.cursor.executemany(sql, rows)
        self.log.info("staged", rows=len(rows))

    def _merge_scorecard(self, stage_table: str) -> int:
        self.cursor.execute(f"""
            MERGE INTO SCORECARDS.LOCATION_SCORECARD AS tgt
            USING {stage_table} AS src
            ON tgt.listing_id = src.listing_id
               AND tgt.score_date = src.score_date
            WHEN MATCHED THEN UPDATE SET
                crime_count_500m_7d     = src.crime_count,
                violent_count_500m_7d   = src.violent_count,
                crime_trend             = src.crime_trend,
                complaint_count_500m_7d = src.complaint_count,
                citizen_incidents_48h   = src.citizen_incidents,
                citizen_nighttime_48h   = src.citizen_nighttime,
                nearby_transit_stops    = src.transit_stops,
                nearby_amenity_count    = src.amenity_count,
                listing_active          = src.listing_active,
                current_price           = src.current_price,
                safety_score            = src.safety_score,
                livability_score        = src.livability_score,
                scoring_metadata        = PARSE_JSON(src.scoring_metadata),
                pipeline_run_id         = src.pipeline_run_id
            WHEN NOT MATCHED THEN INSERT (
                listing_id, score_date,
                crime_count_500m_7d, violent_count_500m_7d, crime_trend,
                complaint_count_500m_7d, citizen_incidents_48h,
                citizen_nighttime_48h, nearby_transit_stops,
                nearby_amenity_count, listing_active, current_price,
                safety_score, livability_score,
                scoring_metadata, pipeline_run_id
            ) VALUES (
                src.listing_id, src.score_date,
                src.crime_count, src.violent_count, src.crime_trend,
                src.complaint_count, src.citizen_incidents,
                src.citizen_nighttime, src.transit_stops,
                src.amenity_count, src.listing_active, src.current_price,
                src.safety_score, src.livability_score,
                PARSE_JSON(src.scoring_metadata), src.pipeline_run_id
            )
        """)
        loaded = self.cursor.rowcount
        self.conn.commit()
        self.log.info("scorecard_merged", loaded=loaded)
        return loaded

    def _update_listing_summary(self):
        """Update LISTING_SUMMARY with today's scores."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        self.cursor.execute(f"""
            MERGE INTO SCORECARDS.LISTING_SUMMARY AS tgt
            USING (
                SELECT
                    l.listing_id, l.source, l.source_url,
                    l.price, l.beds, l.baths, l.sqft,
                    l.street, l.city, l.zip_code, l.neighborhood,
                    l.lat, l.lon, l.description_text,
                    l.primary_photo_url, l.is_current, l.list_date,
                    sc.safety_score,
                    sc.livability_score,
                    sc.scoring_metadata:safety AS safety_metadata,
                    sc.scoring_metadata:livability AS livability_metadata,
                    sc.scoring_metadata:transit:stop_names AS nearest_stops,
                    sc.pipeline_run_id
                FROM RAW.LISTINGS l
                JOIN SCORECARDS.LOCATION_SCORECARD sc
                    ON l.listing_id = sc.listing_id
                    AND sc.score_date = '{today}'::DATE
                WHERE l.is_current = TRUE
            ) AS src
            ON tgt.listing_id = src.listing_id
            WHEN MATCHED THEN UPDATE SET
                source              = src.source,
                source_url          = src.source_url,
                price               = src.price,
                beds                = src.beds,
                baths               = src.baths,
                sqft                = src.sqft,
                street              = src.street,
                city                = src.city,
                zip_code            = src.zip_code,
                neighborhood        = src.neighborhood,
                lat                 = src.lat,
                lon                 = src.lon,
                description_text    = src.description_text,
                primary_photo_url   = src.primary_photo_url,
                is_active           = src.is_current,
                list_date           = src.list_date,
                safety_score        = src.safety_score,
                livability_score    = src.livability_score,
                safety_metadata     = src.safety_metadata,
                livability_metadata = src.livability_metadata,
                nearest_stops       = src.nearest_stops,
                last_scored_at      = CURRENT_TIMESTAMP(),
                pipeline_run_id     = src.pipeline_run_id
            WHEN NOT MATCHED THEN INSERT (
                listing_id, source, source_url,
                price, beds, baths, sqft,
                street, city, zip_code, neighborhood,
                lat, lon, description_text,
                primary_photo_url, is_active, list_date,
                safety_score, livability_score,
                safety_metadata, livability_metadata,
                nearest_stops, last_scored_at, pipeline_run_id
            ) VALUES (
                src.listing_id, src.source, src.source_url,
                src.price, src.beds, src.baths, src.sqft,
                src.street, src.city, src.zip_code, src.neighborhood,
                src.lat, src.lon, src.description_text,
                src.primary_photo_url, src.is_current, src.list_date,
                src.safety_score, src.livability_score,
                src.safety_metadata, src.livability_metadata,
                src.nearest_stops, CURRENT_TIMESTAMP(), src.pipeline_run_id
            )
        """)
        summary_count = self.cursor.rowcount
        self.conn.commit()
        self.log.info("summary_updated", rows=summary_count)

    def _drop_staging(self, stage_table: str):
        self.cursor.execute(f"DROP TABLE IF EXISTS {stage_table}")


if __name__ == "__main__":
    pipeline = ScoringPipeline()
    result = pipeline.run()
    raise SystemExit(0 if result.status == "success" else 1)