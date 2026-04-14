"""Google News RSS neighbourhood intelligence pipeline.

Fetches news articles from Google News RSS feeds matching a preference
tag, extracts full article text via StealthyFetcher, classifies with
two-stage LLM gating, and loads to RAW.LIFESTYLE_SIGNALS.

Two-stage LLM classification:
  Stage 1  Batch relevance filter on titles + text previews.
  Stage 2  Deep per-article analysis producing cited narratives.

Transport: StealthyFetcher for both RSS feeds and article pages.
Handles Google News redirect URLs transparently — Playwright follows
302s to the real article domain.

Zero truncation: full article text is stored in raw_thread_text for
downstream Pinecone embedding.  No text is ever shortened.

Config is agent-writable: the Organizer Agent appends queries to
config/sources/google_news.yml; the next DAG run picks them up.

Usage:
    python -m app.pipelines.ingest_google_news --preference-tag safety --dry-run
    python -m app.pipelines.ingest_google_news --preference-tag korean_food
    python -m app.pipelines.ingest_google_news --preference-tag safety --query "boston crime"
"""

import hashlib
import json
import re
import time
import argparse
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

import structlog

from app.core.base_pipeline import BasePipeline, PipelineRunResult
from app.core.config_loader import load_source_config, load_pipeline, load_classification
from app.core.classifier import ProviderChain, CostTracker

logger = structlog.get_logger()

try:
    from scrapling.fetchers import StealthyFetcher
except ImportError:
    raise RuntimeError(
        "scrapling[fetchers] not installed. "
        "Run: pip install 'scrapling[fetchers]' && scrapling install"
    )


# ── Transport ────────────────────────────────────────────────

class NewsTransport:
    """StealthyFetcher wrapper with backoff retry and delay pacing."""

    def __init__(self, config: dict, retry_config):
        rate = config.get("rate_limit", {})
        self.delay = rate.get("delay_between_requests", 3.0)
        self._backoff_base = rate.get("backoff_base", 2.0)
        self._backoff_max = rate.get("backoff_max", 30.0)
        self._max_attempts = retry_config.max_attempts
        self._log = logger.bind(component="news_transport")

    def fetch_html(self, url: str) -> str | None:
        """Fetch URL via StealthyFetcher with exponential backoff retry.

        Returns raw HTML string or None on failure.
        """
        for attempt in range(1, self._max_attempts + 1):
            try:
                page = StealthyFetcher.fetch(
                    url, headless=True, network_idle=True,
                )
                html = (
                    page.html_content
                    if hasattr(page, "html_content")
                    else str(page)
                )

                if len(html) < 1000:
                    wait = min(
                        self._backoff_base ** attempt, self._backoff_max,
                    )
                    self._log.warning(
                        "page_too_short", url=url[:80],
                        chars=len(html), attempt=attempt, wait_s=wait,
                    )
                    if attempt < self._max_attempts:
                        time.sleep(wait)
                        continue
                    return None

                return html

            except Exception as exc:
                wait = min(
                    self._backoff_base ** attempt, self._backoff_max,
                )
                self._log.warning(
                    "fetch_failed", url=url[:80],
                    attempt=attempt, error=str(exc)[:120], wait_s=wait,
                )
                if attempt < self._max_attempts:
                    time.sleep(wait)

        self._log.error("exhausted_retries", url=url[:80])
        return None

    def fetch_page(self, url: str):
        """Fetch URL and return the Scrapling Response object.

        Callers use .css(), .get_all_text() etc. for structured extraction.
        Returns None on failure.
        """
        for attempt in range(1, self._max_attempts + 1):
            try:
                page = StealthyFetcher.fetch(
                    url, headless=True, network_idle=True,
                )
                html = (
                    page.html_content
                    if hasattr(page, "html_content")
                    else str(page)
                )

                if len(html) < 1000:
                    wait = min(
                        self._backoff_base ** attempt, self._backoff_max,
                    )
                    self._log.warning(
                        "page_too_short", url=url[:80],
                        chars=len(html), attempt=attempt, wait_s=wait,
                    )
                    if attempt < self._max_attempts:
                        time.sleep(wait)
                        continue
                    return None

                return page

            except Exception as exc:
                wait = min(
                    self._backoff_base ** attempt, self._backoff_max,
                )
                self._log.warning(
                    "fetch_failed", url=url[:80],
                    attempt=attempt, error=str(exc)[:120], wait_s=wait,
                )
                if attempt < self._max_attempts:
                    time.sleep(wait)

        self._log.error("exhausted_retries", url=url[:80])
        return None


# ── Extractor ────────────────────────────────────────────────

class NewsExtractor:
    """Fetches Google News RSS feeds and extracts full article text.

    RSS → regex XML parse (StealthyFetcher returns raw XML for RSS URLs).
    Article → StealthyFetcher follows Google News 302 → CSS <p> extraction.
    """

    def __init__(self, config: dict, transport: NewsTransport):
        self._transport = transport
        conn = config.get("connection", {})
        self._rss_base = conn.get(
            "rss_base",
            "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en",
        )
        ext = config.get("extraction", {})
        self._max_articles = ext.get("max_articles_per_query", 10)
        self._selectors = ext.get("article_selectors", ["article", "main"])
        self._min_para_chars = ext.get("min_paragraph_chars", 30)
        self._min_article_chars = ext.get("min_article_chars", 200)
        self._blocked = set(config.get("blocked_sources", []))
        self._log = logger.bind(component="news_extractor")

    # ── RSS ──────────────────────────────────────────────────

    def fetch_rss(self, query: str) -> list[dict]:
        """Fetch Google News RSS and parse XML items into dicts."""
        url = self._rss_base.format(query=quote_plus(query))
        html = self._transport.fetch_html(url)
        if not html:
            return []

        raw_items = re.findall(r"<item>(.*?)</item>", html, re.DOTALL)
        entries = []
        for raw in raw_items[:self._max_articles]:
            entry = self._parse_rss_item(raw)
            if not entry.get("link"):
                continue

            # Skip sources known to block extraction
            if entry.get("source") in self._blocked:
                self._log.debug(
                    "blocked_source_skipped", source=entry["source"],
                )
                continue

            entries.append(entry)

        self._log.info(
            "rss_parsed", query=query[:50],
            raw=len(raw_items), accepted=len(entries),
        )
        return entries

    @staticmethod
    def _parse_rss_item(raw: str) -> dict:
        """Extract fields from a single RSS <item> XML block."""
        title_m = re.search(r"<title>(.*?)</title>", raw)
        link_m = re.search(
            r"(https://news\.google\.com/rss/articles/[^<\s\"']+)", raw,
        )
        source_m = re.search(r"<source[^>]*>(.*?)</source>", raw)
        pub_m = re.search(r"<pubDate>(.*?)</pubDate>", raw)

        return {
            "title": title_m.group(1).strip() if title_m else "",
            "link": link_m.group(1).strip() if link_m else "",
            "source": source_m.group(1).strip() if source_m else "unknown",
            "published": pub_m.group(1).strip() if pub_m else "",
        }

    # ── Article extraction ───────────────────────────────────

    def extract_article(self, entry: dict) -> dict | None:
        """Fetch article page and extract full body text.

        Returns enriched entry dict with article_text, final_url, title,
        published_iso, or None on failure.
        """
        page = self._transport.fetch_page(entry["link"])
        if not page:
            return None

        final_url = getattr(page, "url", entry["link"])
        html = (
            page.html_content
            if hasattr(page, "html_content")
            else str(page)
        )

        # Extract h1 title from the actual page
        title = self._extract_title(page, html) or entry.get("title", "")

        # Extract article body — try structured selectors, then <p> global
        text, method, para_count = self._extract_body(page)

        if not text:
            self._log.warning(
                "extraction_failed",
                source=entry.get("source"), url=final_url[:80],
            )
            return None

        # Parse published date to ISO 8601
        published_iso = self._parse_pub_date(entry.get("published", ""))

        return {
            **entry,
            "final_url": final_url,
            "title": title,
            "article_text": text,
            "extraction_method": method,
            "paragraph_count": para_count,
            "text_len": len(text),
            "published_iso": published_iso,
        }

    def _extract_title(self, page, html: str) -> str:
        """Best-effort h1 extraction, falls back to <title> tag."""
        try:
            h1s = page.css("h1")
            if h1s and len(h1s) > 0:
                t = h1s[0].text.strip()
                if t:
                    return t
        except Exception:
            pass

        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.DOTALL)
        if m:
            return re.sub(r"<[^>]+>", "", m.group(1)).strip()
        return ""

    def _extract_body(self, page) -> tuple[str | None, str, int]:
        """Try CSS selectors → global <p> → get_all_text(). Zero truncation.

        Returns (text, method, paragraph_count).
        """
        # Method 1: CSS selectors for article containers
        for selector in self._selectors:
            try:
                elements = page.css(selector)
                if not elements or len(elements) == 0:
                    continue

                parts = []
                for el in elements:
                    try:
                        paras = el.css("p")
                        if paras and len(paras) > 0:
                            for p in paras:
                                t = (
                                    p.text.strip()
                                    if hasattr(p, "text") else ""
                                )
                                if len(t) >= self._min_para_chars:
                                    parts.append(t)
                    except Exception:
                        try:
                            t = el.get_all_text()
                            if t and len(t) > 100:
                                parts.append(t)
                        except Exception:
                            pass

                if parts:
                    text = "\n\n".join(parts)
                    if len(text) >= self._min_article_chars:
                        return text, f"css:{selector}", len(parts)
            except Exception:
                continue

        # Method 2: all <p> tags globally
        try:
            all_p = page.css("p")
            if all_p and len(all_p) > 0:
                parts = []
                for p in all_p:
                    t = p.text.strip() if hasattr(p, "text") else ""
                    if len(t) > 40:
                        parts.append(t)
                if len(parts) >= 3:
                    text = "\n\n".join(parts)
                    if len(text) >= self._min_article_chars:
                        return text, "p_tags_global", len(parts)
        except Exception:
            pass

        # Method 3: get_all_text (noisiest fallback)
        try:
            full = page.get_all_text()
            if full and len(full) >= self._min_article_chars:
                return full, "get_all_text", 0
        except Exception:
            pass

        return None, "", 0

    @staticmethod
    def _parse_pub_date(raw: str) -> str | None:
        """Parse RFC 2822 date from RSS into ISO 8601."""
        if not raw:
            return None
        try:
            dt = parsedate_to_datetime(raw)
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            return None


# ── Classifier ───────────────────────────────────────────────

class NewsClassifier:
    """Two-stage LLM classification for news articles.

    Stage 1  Batch relevance filter on titles + text previews.
    Stage 2  Deep per-article analysis producing cited narratives.
    """

    def __init__(self, cursor, pipeline_run_id: str):
        config = load_classification()
        prompts = config.get("prompts", {})

        if "google_news_filter" not in prompts:
            raise ValueError(
                "Missing 'google_news_filter' prompt in classification.yml"
            )
        if "google_news" not in prompts:
            raise ValueError(
                "Missing 'google_news' prompt in classification.yml"
            )

        self._filter_prompt = prompts["google_news_filter"]["system"]
        self._deep_prompt = prompts["google_news"]["system"]
        self._chain = ProviderChain()
        self._cost = CostTracker(cursor, pipeline_run_id, "google_news")
        self._log = logger.bind(component="news_classifier")

    # ── Stage 1: batch filter ────────────────────────────────

    def stage1_filter(
        self,
        articles: list[dict],
        preference_tag: str,
        query: str,
    ) -> list[dict]:
        """Batch-score articles. Attaches _stage1_score to each dict."""
        if not articles:
            return []

        items = [
            {
                "index": i,
                "title": a["title"],
                "source": a.get("source", ""),
                "published": a.get("published_iso") or a.get("published", ""),
                "text_preview": (a.get("article_text") or "")[:600],
            }
            for i, a in enumerate(articles)
        ]

        user_prompt = json.dumps({
            "preference_tag": preference_tag,
            "query": query,
            "articles": items,
        })

        try:
            result = self._chain.complete(
                system=self._filter_prompt, user=user_prompt,
            )
            self._cost.log_usage(result, "google_news_filter", len(articles))
            scores = self._parse_filter(result["content"], len(articles))

            for i, a in enumerate(articles):
                a["_stage1_score"] = scores.get(i, 0)

        except Exception as exc:
            self._log.error("stage1_failed", error=str(exc))
            for a in articles:
                a["_stage1_score"] = 50  # fail-open

        return articles

    def _parse_filter(self, content: str, count: int) -> dict[int, int]:
        """Parse batch scores from LLM response."""
        clean = self._strip_fences(content)
        try:
            parsed = json.loads(clean)
            if isinstance(parsed, list):
                return {
                    int(item.get("index", i)): int(
                        item.get("relevance_score", 0),
                    )
                    for i, item in enumerate(parsed)
                    if isinstance(item, dict)
                }
            if isinstance(parsed, dict):
                return {int(k): int(v) for k, v in parsed.items()}
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            self._log.warning("filter_parse_failed", error=str(exc))
        return {}

    # ── Stage 2: deep classify ───────────────────────────────

    def stage2_classify(
        self,
        article: dict,
        preference_tag: str,
        query: str,
    ) -> dict | None:
        """Deep analysis of a single article. Returns classification dict."""
        user_prompt = json.dumps({
            "title": article.get("title", ""),
            "source": article.get("source", ""),
            "published": article.get("published_iso") or article.get("published", ""),
            "url": article.get("final_url") or article.get("link", ""),
            "article_text": article.get("article_text") or "",
            "preference_tag": preference_tag,
            "query": query,
        })

        try:
            result = self._chain.complete(
                system=self._deep_prompt, user=user_prompt,
            )
            self._cost.log_usage(result, "google_news_classify", 1)
            return self._parse_deep(result["content"])
        except Exception as exc:
            self._log.error(
                "stage2_failed",
                title=article.get("title", "")[:60], error=str(exc),
            )
            return None

    def _parse_deep(self, content: str) -> dict | None:
        clean = self._strip_fences(content)
        try:
            parsed = json.loads(clean)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError as exc:
            self._log.warning(
                "deep_parse_failed",
                error=str(exc), preview=clean[:200],
            )
        return None

    # ── Helpers ──────────────────────────────────────────────

    @staticmethod
    def _strip_fences(text: str) -> str:
        clean = text.strip()
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[1] if "\n" in clean else clean[3:]
        if clean.endswith("```"):
            clean = clean[:-3]
        return clean.strip()


# ── Pipeline ─────────────────────────────────────────────────

class GoogleNewsPipeline(BasePipeline):
    """Orchestrates RSS fetch → extract → filter → classify → load."""

    SOURCE = "google_news"
    DESCRIPTION = "Ingest Google News articles as neighbourhood signals"

    def __init__(self):
        super().__init__()
        self._config = load_source_config("google_news")
        self._pipeline_config = load_pipeline()

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser):
        parser.add_argument(
            "--preference-tag", type=str, required=True,
            help="Tag to query (e.g. safety, korean_food).",
        )
        parser.add_argument(
            "--query", type=str, default=None,
            help="Single query override. Default: all queries from config.",
        )

    def run_pipeline(self, args: argparse.Namespace) -> PipelineRunResult:
        tag = args.preference_tag
        cfg = self._config
        retry = self._pipeline_config["retry"]

        # Resolve queries for this tag
        if args.query:
            queries = [args.query]
        else:
            queries = cfg.get("queries", {}).get(tag, [])
            if not queries:
                queries = [f"boston {tag.replace('_', ' ')}"]

        relevance_cfg = cfg.get("relevance", {})
        s1_thresh = relevance_cfg.get("stage1_threshold", 30)
        s2_thresh = relevance_cfg.get("stage2_threshold", 40)

        transport = NewsTransport(cfg, retry)
        extractor = NewsExtractor(cfg, transport)
        classifier = NewsClassifier(self.cursor, self.pipeline_run_id)

        # ── Fetch RSS across all queries ─────────────────────
        all_entries: list[dict] = []
        for q in queries:
            entries = extractor.fetch_rss(q)
            for e in entries:
                e["_query"] = q
            all_entries.extend(entries)
            if entries:
                time.sleep(transport.delay)

        # Deduplicate by Google News article link
        seen: set[str] = set()
        unique: list[dict] = []
        for e in all_entries:
            link = e.get("link", "")
            if link and link not in seen:
                seen.add(link)
                unique.append(e)

        self.log.info(
            "rss_complete", tag=tag, queries=len(queries),
            raw=len(all_entries), unique=len(unique),
        )

        if not unique:
            return PipelineRunResult(
                pipeline_run_id=self.pipeline_run_id,
                source=self.SOURCE,
                records_extracted=0,
            )

        # ── Extract full article text ────────────────────────
        articles: list[dict] = []
        for i, entry in enumerate(unique):
            article = extractor.extract_article(entry)
            if article:
                articles.append(article)
            else:
                self.record_error(
                    record_key=entry.get("link", "")[:100],
                    error_type="extraction",
                    error_message=f"Failed to extract: {entry.get('source', '?')}",
                )
            if i < len(unique) - 1:
                time.sleep(transport.delay)

        self.log.info(
            "extraction_complete",
            attempted=len(unique), extracted=len(articles),
        )

        if not articles:
            return PipelineRunResult(
                pipeline_run_id=self.pipeline_run_id,
                source=self.SOURCE,
                records_extracted=len(unique),
            )

        # ── Stage 1: batch LLM relevance filter ─────────────
        scored = classifier.stage1_filter(articles, tag, queries[0])
        relevant = [
            a for a in scored
            if a.get("_stage1_score", 0) >= s1_thresh
        ]

        self.log.info(
            "stage1_complete", total=len(articles),
            relevant=len(relevant), threshold=s1_thresh,
        )

        if not relevant:
            return PipelineRunResult(
                pipeline_run_id=self.pipeline_run_id,
                source=self.SOURCE,
                records_extracted=len(unique),
            )

        # ── Stage 2: deep LLM classification ─────────────────
        classified: list[dict] = []
        for article in relevant:
            classification = classifier.stage2_classify(
                article, tag, article.get("_query", queries[0]),
            )
            if not classification:
                self.record_error(
                    record_key=article.get("final_url", "")[:100],
                    error_type="classification",
                    error_message="Stage 2 LLM returned None",
                )
                continue

            score = classification.get("relevance_score", 0)
            if score < s2_thresh:
                self.record_error(
                    record_key=article.get("final_url", "")[:100],
                    error_type="relevance",
                    error_message=f"stage2_score={score} < threshold={s2_thresh}",
                )
                continue

            article["_classification"] = classification
            classified.append(article)

        self.log.info(
            "stage2_complete", relevant=len(relevant),
            classified=len(classified), threshold=s2_thresh,
        )

        # ── Transform ────────────────────────────────────────
        transformed = [self._transform(a, tag) for a in classified]
        transformed = [r for r in transformed if r]

        if args.dry_run:
            self.log.info(
                "dry_run_complete",
                rss_entries=len(unique),
                extracted=len(articles),
                stage1_relevant=len(relevant),
                stage2_classified=len(classified),
                transformed=len(transformed),
            )
            for r in transformed[:3]:
                self.log.info(
                    "sample",
                    title=r["title"][:80],
                    relevance=r["relevance_score"],
                    sentiment=r["sentiment"],
                    narrative=r["snippet_text"][:300],
                )
            return PipelineRunResult(
                pipeline_run_id=self.pipeline_run_id,
                source=self.SOURCE,
                records_extracted=len(unique),
                records_loaded=0,
            )

        # ── Load ─────────────────────────────────────────────
        stage_table = self._create_staging_table()
        self._stage_batch(stage_table, transformed)
        loaded = self._merge(stage_table)
        self._drop_staging_table(stage_table)

        return PipelineRunResult(
            pipeline_run_id=self.pipeline_run_id,
            source=self.SOURCE,
            records_extracted=len(unique),
            records_loaded=loaded,
            records_skipped=len(classified) - len(transformed),
            records_failed=len(unique) - len(classified),
        )

    # ── Transform ────────────────────────────────────────────

    def _transform(self, article: dict, preference_tag: str) -> dict | None:
        """Map extracted + classified article to LIFESTYLE_SIGNALS schema."""
        cls = article.get("_classification", {})
        title = article.get("title") or ""
        final_url = article.get("final_url") or article.get("link", "")
        article_text = article.get("article_text") or ""

        if not title or not final_url:
            return None

        narrative = cls.get("narrative", "")

        # Deterministic signal_id: source + URL hash + tag
        signal_id = hashlib.sha256(
            f"google_news:{final_url}:{preference_tag}".encode(),
        ).hexdigest()[:64]

        # Content hash on full article text
        content_hash = hashlib.sha256(
            f"{title}|{article_text}".encode(),
        ).hexdigest()

        # Use parsed ISO date from RSS, fallback to classification date
        published_iso = article.get("published_iso")

        return {
            "signal_id": signal_id,
            "signal_source": "google_news",
            "source_native_id": final_url[:100],
            "preference_tag": preference_tag,
            "title": title,
            "snippet_text": narrative,
            "raw_thread_text": article_text,
            "url": final_url,
            "content_hash": content_hash,
            "sentiment": cls.get("sentiment"),
            "relevance_score": cls.get("relevance_score"),
            "lat": None,
            "lon": None,
            "classification_metadata": json.dumps({
                "category": cls.get("category"),
                "topics": cls.get("topics", []),
                "neighborhoods_mentioned": cls.get(
                    "neighborhoods_mentioned", [],
                ),
                "key_facts": cls.get("key_facts", []),
                "news_source": article.get("source", ""),
                "published_date": published_iso,
                "extraction_method": article.get("extraction_method", ""),
                "paragraph_count": article.get("paragraph_count", 0),
                "article_chars": article.get("text_len", 0),
                "stage1_score": article.get("_stage1_score"),
                "stage2_score": cls.get("relevance_score"),
                "query_used": article.get("_query", ""),
            }),
            "pipeline_run_id": self.pipeline_run_id,
        }

    # ── Staging + Merge ──────────────────────────────────────

    def _create_staging_table(self) -> str:
        batch_id = self.pipeline_run_id[:8]
        table = f"RAW.GNEWS_STAGING_{batch_id}"

        self.cursor.execute(f"""
            CREATE TEMPORARY TABLE {table} (
                signal_id               VARCHAR(64),
                signal_source           VARCHAR(30),
                source_native_id        VARCHAR(100),
                preference_tag          VARCHAR(50),
                title                   TEXT,
                snippet_text            TEXT,
                raw_thread_text         TEXT,
                url                     TEXT,
                content_hash            VARCHAR(64),
                sentiment               VARCHAR(20),
                relevance_score         INT,
                lat                     FLOAT,
                lon                     FLOAT,
                classification_metadata TEXT,
                pipeline_run_id         VARCHAR(36)
            )
        """)
        self.log.info("staging_created", table=table)
        return table

    def _stage_batch(self, stage_table: str, records: list[dict]):
        sql = f"""
            INSERT INTO {stage_table} (
                signal_id, signal_source, source_native_id, preference_tag,
                title, snippet_text, raw_thread_text, url, content_hash,
                sentiment, relevance_score, lat, lon,
                classification_metadata, pipeline_run_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                      %s, %s, %s, %s, %s, %s)
        """
        rows = [
            (
                r["signal_id"], r["signal_source"], r["source_native_id"],
                r["preference_tag"], r["title"], r["snippet_text"],
                r["raw_thread_text"], r["url"], r["content_hash"],
                r["sentiment"], r["relevance_score"], r["lat"], r["lon"],
                r["classification_metadata"], r["pipeline_run_id"],
            )
            for r in records
        ]
        self.cursor.executemany(sql, rows)

    def _merge(self, stage_table: str) -> int:
        self.cursor.execute(f"""
            MERGE INTO RAW.LIFESTYLE_SIGNALS AS target
            USING {stage_table} AS src
            ON target.signal_id = src.signal_id
            WHEN MATCHED THEN UPDATE SET
                title                   = src.title,
                snippet_text            = src.snippet_text,
                raw_thread_text         = src.raw_thread_text,
                url                     = src.url,
                content_hash            = src.content_hash,
                sentiment               = src.sentiment,
                relevance_score         = src.relevance_score,
                classification_metadata = PARSE_JSON(src.classification_metadata),
                pipeline_run_id         = src.pipeline_run_id,
                fetched_at              = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT (
                signal_id, signal_source, source_native_id, preference_tag,
                title, snippet_text, raw_thread_text, url, content_hash,
                sentiment, relevance_score, lat, lon,
                classification_metadata, pipeline_run_id
            ) VALUES (
                src.signal_id, src.signal_source, src.source_native_id,
                src.preference_tag, src.title, src.snippet_text,
                src.raw_thread_text, src.url, src.content_hash, src.sentiment,
                src.relevance_score, src.lat, src.lon,
                PARSE_JSON(src.classification_metadata), src.pipeline_run_id
            )
        """)
        loaded = self.cursor.rowcount
        self.conn.commit()
        self.log.info("merge_complete", loaded=loaded)
        return loaded

    def _drop_staging_table(self, stage_table: str):
        self.cursor.execute(f"DROP TABLE IF EXISTS {stage_table}")


if __name__ == "__main__":
    pipeline = GoogleNewsPipeline()
    result = pipeline.run()
    raise SystemExit(0 if result.status == "success" else 1)
