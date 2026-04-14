"""Reddit neighbourhood intelligence pipeline.

Dual-purpose ingestion of Reddit threads into RAW.LIFESTYLE_SIGNALS:

  Livability   (safety, noise, parking …)  → searched within hardcoded
               Boston-area subreddits with restrict_sr.
  Lifestyle    (yoga, korean_food, live_music …) → global Reddit search
               with location keyword appended.

Two-stage LLM classification gates what gets loaded:
  Stage 1  batch relevance filter on titles      → skip irrelevant posts
  Stage 2  deep per-thread analysis with evidence → rich cited narratives

Transport: httpx primary (fast JSON API), StealthyFetcher fallback
on persistent rate limits.  Per-run request budget prevents overuse.

Config is agent-writable: the Organizer Agent appends subreddits and
queries to config/sources/reddit.yml; the next DAG run picks them up.

Usage:
    python -m app.pipelines.ingest_reddit --preference-tag safety --dry-run
    python -m app.pipelines.ingest_reddit --preference-tag live_music --dry-run
    python -m app.pipelines.ingest_reddit --preference-tag safety --query "safe at night" --dry-run
    python -m app.pipelines.ingest_reddit --preference-tag yoga --subreddit boston --dry-run
"""

import hashlib
import json
import re
import time
import argparse
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
import structlog

from app.core.base_pipeline import BasePipeline, PipelineRunResult
from app.core.config_loader import load_source_config, load_classification
from app.core.classifier import ProviderChain, CostTracker

logger = structlog.get_logger()

try:
    from scrapling.fetchers import StealthyFetcher
    _HAS_STEALTH = True
except ImportError:
    _HAS_STEALTH = False


# ── Exceptions ───────────────────────────────────────────────

class BudgetExhausted(Exception):
    """Per-run HTTP request budget depleted."""


# ── Transport ────────────────────────────────────────────────

class RedditTransport:
    """Dual-transport Reddit HTTP client.

    Primary:  httpx → Reddit public JSON API (fast, clean).
    Fallback: StealthyFetcher on 3+ consecutive 429s (browser-grade
              TLS fingerprint bypasses IP-based rate limits).

    Tracks every request against a per-run budget.  Four-tier backoff:
      T0  hard budget cap
      T1  base delay between all requests (configurable)
      T2  exponential backoff on 429 (2^attempt, max 60 s)
      T3  switch to StealthyFetcher after 3 consecutive 429s
    """

    def __init__(self, config: dict):
        conn = config.get("connection", {})
        self._timeout = conn.get("timeout", 15)
        self._headers = {"User-Agent": conn.get(
            "user_agent",
            "Vicinity/1.0 (educational research)",
        )}

        rate = config.get("rate_limit", {})
        self.delay = rate.get("delay_between_requests", 2.0)
        self._backoff_base = rate.get("backoff_base", 2.0)
        self._backoff_max = rate.get("backoff_max", 60.0)
        self._max_attempts = rate.get("max_attempts", 3)
        self._budget = config.get("max_requests_per_run", 60)
        self._request_count = 0
        self._consecutive_429s = 0
        self._use_stealth = False

        self._log = logger.bind(component="reddit_transport")

    @property
    def requests_remaining(self) -> int:
        return max(0, self._budget - self._request_count)

    # ── Public API ───────────────────────────────────────────

    def get_json(self, url: str, params: dict | None = None) -> dict | None:
        """Fetch JSON with budget tracking and automatic transport fallback."""
        if self._request_count >= self._budget:
            raise BudgetExhausted(
                f"Request budget exhausted ({self._budget} calls)"
            )
        self._request_count += 1

        if self._use_stealth and _HAS_STEALTH:
            return self._stealth_fetch(url, params)
        return self._httpx_fetch(url, params)

    # ── httpx (primary) ──────────────────────────────────────

    def _httpx_fetch(self, url: str, params: dict | None) -> dict | None:
        for attempt in range(1, self._max_attempts + 1):
            try:
                resp = httpx.get(
                    url, params=params, headers=self._headers,
                    timeout=self._timeout, follow_redirects=True,
                )

                if resp.status_code == 200:
                    self._consecutive_429s = 0
                    ct = resp.headers.get("content-type", "")
                    if "json" in ct:
                        return resp.json()
                    self._log.warning("non_json_response",
                                      url=url[:80], ct=ct[:40])
                    return None

                if resp.status_code == 429:
                    self._consecutive_429s += 1
                    wait = min(
                        self._backoff_base ** attempt, self._backoff_max,
                    )
                    self._log.warning(
                        "rate_limited", attempt=attempt, wait_s=wait,
                        consecutive=self._consecutive_429s,
                    )
                    # Tier 3: persistent 429 → switch transport
                    if self._consecutive_429s >= 3 and _HAS_STEALTH:
                        self._log.info("switching_to_stealth")
                        self._use_stealth = True
                        return self._stealth_fetch(url, params)
                    time.sleep(wait)
                    continue

                if resp.status_code == 403:
                    self._log.error("forbidden_aborting", url=url[:80])
                    return None

                self._log.error("http_error",
                                status=resp.status_code, url=url[:80])
                return None

            except (httpx.TimeoutException, httpx.RequestError) as exc:
                wait = min(self._backoff_base ** attempt, self._backoff_max)
                self._log.warning("request_failed", error=str(exc),
                                  attempt=attempt, wait_s=wait)
                if attempt < self._max_attempts:
                    time.sleep(wait)

        self._log.error("exhausted_retries", url=url[:80])
        return None

    # ── StealthyFetcher (fallback) ───────────────────────────

    def _stealth_fetch(self, url: str, params: dict | None) -> dict | None:
        full_url = url
        if params:
            sep = "&" if "?" in url else "?"
            full_url += sep + urlencode(params)

        try:
            page = StealthyFetcher.fetch(
                full_url, headless=True, network_idle=True,
            )
            text = (
                page.html_content
                if hasattr(page, "html_content")
                else str(page)
            )

            # Try direct JSON parse (browser may serve raw JSON)
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                pass

            # Try extracting JSON from <pre> tag
            pre = re.search(r"<pre[^>]*>(.*?)</pre>", text, re.DOTALL)
            if pre:
                try:
                    return json.loads(pre.group(1))
                except json.JSONDecodeError:
                    pass

            # Try raw_decode on body text
            body = re.search(r"<body[^>]*>(.*?)</body>", text, re.DOTALL)
            if body:
                try:
                    data, _ = json.JSONDecoder().raw_decode(
                        body.group(1).strip(),
                    )
                    return data
                except (json.JSONDecodeError, ValueError):
                    pass

            self._log.error("stealth_json_parse_failed",
                            url=url[:80], html_len=len(text))
            return None

        except Exception as exc:
            self._log.error("stealth_fetch_failed",
                            url=url[:80], error=str(exc))
            return None


# ── Extractor ────────────────────────────────────────────────

class RedditExtractor:
    """Searches Reddit and fetches full threads with recursive comment trees.

    Two search strategies driven by preference_tag:
      Livability tags → restrict_sr within each configured subreddit
      Lifestyle tags  → unrestricted search, location keyword appended
    """

    def __init__(self, config: dict, transport: RedditTransport):
        self._transport = transport
        self._base_url = config.get("connection", {}).get(
            "base_url", "https://old.reddit.com",
        )

        search = config.get("search", {})
        self._sort = search.get("sort", "relevance")
        self._time_filter = search.get("time_filter", "year")
        self._posts_per_query = search.get("posts_per_query", 10)
        self._min_score = search.get("min_score", 2)
        self._min_comments = search.get("min_comments", 1)

        comments = config.get("comments", {})
        self._comment_limit = comments.get("limit", 200)
        self._comment_sort = comments.get("sort", "top")
        self._comment_min_score = comments.get("min_score", 1)
        self._comment_max_depth = comments.get("max_depth")

        self._livability_subs = config.get(
            "livability_subreddits", ["boston"],
        )
        self._location_kw = config.get("location_keyword", "boston")
        self._livability_tags = set(config.get("livability_tags", []))

        self._log = logger.bind(component="reddit_extractor")

    # ── Search ───────────────────────────────────────────────

    def search_posts(
        self,
        preference_tag: str,
        query: str,
        subreddit_override: str | None = None,
    ) -> list[dict]:
        """Search Reddit, deduplicate, and filter by quality thresholds."""
        all_posts: list[dict] = []

        if subreddit_override:
            all_posts.extend(
                self._search_one(subreddit_override, query, restrict_sr=True),
            )
        elif preference_tag in self._livability_tags:
            for sub in self._livability_subs:
                if self._transport.requests_remaining < 5:
                    self._log.warning("budget_low_stopping_search",
                                      remaining=self._transport.requests_remaining)
                    break
                all_posts.extend(
                    self._search_one(sub, query, restrict_sr=True),
                )
                if all_posts:
                    time.sleep(self._transport.delay)
        else:
            augmented = f"{query} {self._location_kw}"
            all_posts.extend(
                self._search_one(None, augmented, restrict_sr=False),
            )

        # Deduplicate by Reddit fullname (t3_xxx)
        seen: set[str] = set()
        unique: list[dict] = []
        for p in all_posts:
            pid = p.get("id")
            if pid and pid not in seen:
                seen.add(pid)
                unique.append(p)

        # Quality filter
        filtered = [
            p for p in unique
            if p.get("score", 0) >= self._min_score
            and p.get("num_comments", 0) >= self._min_comments
        ]

        self._log.info("search_complete", tag=preference_tag,
                        query=query[:40], raw=len(all_posts),
                        unique=len(unique), after_filter=len(filtered))
        return filtered

    def _search_one(
        self,
        subreddit: str | None,
        query: str,
        restrict_sr: bool,
    ) -> list[dict]:
        if subreddit:
            url = f"{self._base_url}/r/{subreddit}/search.json"
        else:
            url = f"{self._base_url}/search.json"

        params = {
            "q": query,
            "sort": self._sort,
            "t": self._time_filter,
            "limit": self._posts_per_query,
        }
        if restrict_sr:
            params["restrict_sr"] = "on"

        data = self._transport.get_json(url, params=params)
        if not data or not isinstance(data, dict):
            return []

        posts: list[dict] = []
        for child in data.get("data", {}).get("children", []):
            if child.get("kind") != "t3":
                continue
            d = child["data"]
            posts.append({
                "id": d.get("name", ""),
                "post_id": d.get("id", ""),
                "title": d.get("title", ""),
                "selftext": d.get("selftext", ""),
                "score": d.get("score", 0),
                "num_comments": d.get("num_comments", 0),
                "permalink": d.get("permalink", ""),
                "subreddit": d.get("subreddit", ""),
                "created_utc": d.get("created_utc", 0),
                "is_self": d.get("is_self", True),
            })

        label = f"r/{subreddit}" if subreddit else "global"
        self._log.info("subreddit_searched", sub=label,
                        query=query[:40], results=len(posts))
        return posts

    # ── Thread fetch ─────────────────────────────────────────

    def fetch_thread(self, permalink: str) -> dict | None:
        """Fetch full post body + recursively flattened comment tree."""
        url = f"{self._base_url}{permalink}.json"
        params = {"limit": self._comment_limit, "sort": self._comment_sort}

        data = self._transport.get_json(url, params=params)
        if not data or not isinstance(data, list) or len(data) < 2:
            return None

        post_children = data[0].get("data", {}).get("children", [])
        if not post_children:
            return None
        post = post_children[0].get("data", {})

        comment_children = data[1].get("data", {}).get("children", [])
        comments = self._flatten_comments(comment_children, depth=0)

        return {
            "post": {
                "id": post.get("name", ""),
                "post_id": post.get("id", ""),
                "title": post.get("title", ""),
                "selftext": post.get("selftext", ""),
                "score": post.get("score", 0),
                "num_comments": post.get("num_comments", 0),
                "permalink": post.get("permalink", ""),
                "subreddit": post.get("subreddit", ""),
                "created_utc": post.get("created_utc", 0),
            },
            "comments": comments,
        }

    def _flatten_comments(
        self, children: list, depth: int,
    ) -> list[dict]:
        """Recursively walk the Reddit reply tree, extracting scored comments."""
        flat: list[dict] = []
        max_d = self._comment_max_depth

        for child in children:
            if child.get("kind") != "t1":
                continue
            d = child["data"]

            score = d.get("score", 0)
            if score < self._comment_min_score:
                continue
            if max_d is not None and depth > max_d:
                continue

            body = (d.get("body") or "").strip()
            if not body or body in ("[deleted]", "[removed]"):
                continue

            flat.append({
                "body": body,
                "score": score,
                "author": d.get("author", "[deleted]"),
                "created_utc": d.get("created_utc", 0),
                "depth": depth,
            })

            replies = d.get("replies")
            if isinstance(replies, dict):
                reply_children = replies.get("data", {}).get("children", [])
                flat.extend(self._flatten_comments(reply_children, depth + 1))

        return flat


# ── Classifier ───────────────────────────────────────────────

class RedditClassifier:
    """Two-stage LLM classification for Reddit threads.

    Stage 1  Batch relevance filter on post titles + previews.
             Cheap: one LLM call per search-result page.
    Stage 2  Deep per-thread analysis producing evidence-cited narratives.
             Expensive: one LLM call per thread.
    """

    def __init__(self, cursor, pipeline_run_id: str):
        config = load_classification()
        prompts = config.get("prompts", {})

        if "reddit_filter" not in prompts:
            raise ValueError(
                "Missing 'reddit_filter' prompt in classification.yml"
            )
        if "reddit" not in prompts:
            raise ValueError(
                "Missing 'reddit' prompt in classification.yml"
            )

        self._filter_prompt = prompts["reddit_filter"]["system"]
        self._deep_prompt = prompts["reddit"]["system"]

        self._chain = ProviderChain()
        self._cost = CostTracker(cursor, pipeline_run_id, "reddit")
        self._log = logger.bind(component="reddit_classifier")

    # ── Stage 1 ──────────────────────────────────────────────

    def stage1_filter(
        self,
        posts: list[dict],
        preference_tag: str,
        query: str,
    ) -> list[dict]:
        """Batch-score posts. Attaches _stage1_score to each post dict."""
        if not posts:
            return []

        items = [
            {
                "index": i,
                "title": p["title"],
                "selftext_preview": p.get("selftext") or "",
                "subreddit": p.get("subreddit", ""),
                "score": p.get("score", 0),
                "num_comments": p.get("num_comments", 0),
            }
            for i, p in enumerate(posts)
        ]

        user_prompt = json.dumps({
            "preference_tag": preference_tag,
            "query": query,
            "posts": items,
        })

        try:
            result = self._chain.complete(
                system=self._filter_prompt, user=user_prompt,
            )
            self._cost.log_usage(result, "reddit_filter", len(posts))
            scores = self._parse_filter(result["content"], len(posts))

            for i, p in enumerate(posts):
                p["_stage1_score"] = scores.get(i, 0)

        except Exception as exc:
            self._log.error("stage1_failed", error=str(exc))
            for p in posts:
                p["_stage1_score"] = 50  # fail-open

        return posts

    def _parse_filter(self, content: str, count: int) -> dict[int, int]:
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

    # ── Stage 2 ──────────────────────────────────────────────

    def stage2_classify(
        self,
        thread: dict,
        preference_tag: str,
        query: str,
    ) -> dict | None:
        """Deep analysis of full thread. Returns rich classification dict."""
        post = thread["post"]
        comments = thread["comments"]

        post_date = self._epoch_to_date(post.get("created_utc", 0))

        comment_input = [
            {
                "body": c["body"],
                "score": c["score"],
                "date": self._epoch_to_date(c.get("created_utc", 0)),
                "depth": c.get("depth", 0),
            }
            for c in comments  # full comment tree, zero truncation
        ]

        user_prompt = json.dumps({
            "post_title": post.get("title", ""),
            "post_body": post.get("selftext") or "",
            "post_score": post.get("score", 0),
            "post_date": post_date,
            "subreddit": post.get("subreddit", ""),
            "num_comments": post.get("num_comments", 0),
            "comments": comment_input,
            "preference_tag": preference_tag,
            "query": query,
        })

        try:
            result = self._chain.complete(
                system=self._deep_prompt, user=user_prompt,
            )
            self._cost.log_usage(result, "reddit_classify", 1)
            return self._parse_deep(result["content"])

        except Exception as exc:
            self._log.error("stage2_failed",
                            post_id=post.get("id"), error=str(exc))
            return None

    def _parse_deep(self, content: str) -> dict | None:
        clean = self._strip_fences(content)
        try:
            parsed = json.loads(clean)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError as exc:
            self._log.warning("deep_parse_failed",
                              error=str(exc), preview=clean[:200])
        return None

    # ── Helpers ───────────────────────────────────────────────

    @staticmethod
    def _strip_fences(text: str) -> str:
        clean = text.strip()
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[1] if "\n" in clean else clean[3:]
        if clean.endswith("```"):
            clean = clean[:-3]
        return clean.strip()

    @staticmethod
    def _epoch_to_date(epoch: float) -> str:
        if not epoch:
            return ""
        return datetime.fromtimestamp(
            epoch, tz=timezone.utc,
        ).strftime("%Y-%m-%d")


# ── Pipeline ─────────────────────────────────────────────────

class RedditPipeline(BasePipeline):
    """Orchestrates search → filter → fetch → classify → load."""

    SOURCE = "reddit"
    DESCRIPTION = "Ingest Reddit threads as lifestyle and livability signals"

    def __init__(self):
        super().__init__()
        self._config = load_source_config("reddit")

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser):
        parser.add_argument(
            "--preference-tag", type=str, required=True,
            help="Tag to query (e.g. safety, live_music).",
        )
        parser.add_argument(
            "--query", type=str, default=None,
            help="Single query override. Default: all queries from config.",
        )
        parser.add_argument(
            "--subreddit", type=str, default=None,
            help="Force search within this single subreddit.",
        )

    def run_pipeline(self, args: argparse.Namespace) -> PipelineRunResult:
        tag = args.preference_tag
        cfg = self._config
        # ── Resolve queries ──────────────────────────────────
        if args.query:
            queries = [args.query]
        else:
            queries = cfg.get("queries", {}).get(tag, [])
            if not queries:
                queries = [tag.replace("_", " ")]

        relevance_cfg = cfg.get("relevance", {})
        s1_thresh = relevance_cfg.get("stage1_threshold", 30)
        s2_thresh = relevance_cfg.get("stage2_threshold", 40)

        transport = RedditTransport(cfg)
        extractor = RedditExtractor(cfg, transport)
        classifier = RedditClassifier(self.cursor, self.pipeline_run_id)

        # ── Search across all queries for this tag ───────────
        all_posts: list[dict] = []
        for q in queries:
            try:
                posts = extractor.search_posts(tag, q, args.subreddit)
                all_posts.extend(posts)
            except BudgetExhausted:
                self.log.warning("budget_exhausted_during_search")
                break
            if posts:
                time.sleep(transport.delay)

        # Deduplicate across queries
        seen: set[str] = set()
        unique: list[dict] = []
        for p in all_posts:
            pid = p.get("id")
            if pid and pid not in seen:
                seen.add(pid)
                unique.append(p)

        if not unique:
            self.log.info("no_posts", tag=tag, queries=queries)
            return PipelineRunResult(
                pipeline_run_id=self.pipeline_run_id,
                source=self.SOURCE,
                records_extracted=0,
            )

        # ── Stage 1: batch LLM relevance filter ─────────────
        scored = classifier.stage1_filter(unique, tag, queries[0])
        relevant = [
            p for p in scored
            if p.get("_stage1_score", 0) >= s1_thresh
        ]

        self.log.info("stage1_complete", total=len(unique),
                       relevant=len(relevant), threshold=s1_thresh)

        if not relevant:
            return PipelineRunResult(
                pipeline_run_id=self.pipeline_run_id,
                source=self.SOURCE,
                records_extracted=len(unique),
            )

        # ── Fetch full threads ───────────────────────────────
        threads: list[dict] = []
        for post in relevant:
            permalink = post.get("permalink")
            if not permalink:
                continue
            try:
                thread = extractor.fetch_thread(permalink)
                if thread:
                    thread["_stage1_score"] = post.get("_stage1_score", 0)
                    thread["_query"] = queries[0]
                    threads.append(thread)
            except BudgetExhausted:
                self.log.warning("budget_exhausted_during_fetch",
                                  fetched=len(threads))
                break
            time.sleep(transport.delay)

        self.log.info("threads_fetched", count=len(threads))

        # ── Stage 2: deep LLM classification ─────────────────
        classified: list[dict] = []
        for thread in threads:
            classification = classifier.stage2_classify(
                thread, tag, thread.get("_query", queries[0]),
            )

            if not classification:
                self.record_error(
                    record_key=thread["post"].get("id"),
                    error_type="classification",
                    error_message="Stage 2 LLM classification returned None",
                )
                continue

            score = classification.get("relevance_score", 0)
            if score < s2_thresh:
                self.record_error(
                    record_key=thread["post"].get("id"),
                    error_type="relevance",
                    error_message=(
                        f"stage2_score={score} < threshold={s2_thresh}"
                    ),
                )
                continue

            thread["_classification"] = classification
            classified.append(thread)

        self.log.info("stage2_complete", threads=len(threads),
                       classified=len(classified), threshold=s2_thresh)

        # ── Transform ────────────────────────────────────────
        transformed = [self._transform(t, tag) for t in classified]
        transformed = [r for r in transformed if r]

        if args.dry_run:
            self.log.info(
                "dry_run_complete",
                posts_searched=len(unique),
                stage1_relevant=len(relevant),
                threads_fetched=len(threads),
                stage2_classified=len(classified),
                transformed=len(transformed),
            )
            # Print sample narratives for manual review
            for r in transformed[:3]:
                self.log.info(
                    "sample_narrative",
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

    def _transform(self, thread: dict, preference_tag: str) -> dict | None:
        post = thread["post"]
        cls = thread["_classification"]
        comments = thread.get("comments", [])

        post_id = post.get("id", "")
        title = post.get("title") or ""
        if not post_id or not title:
            return None

        narrative = cls.get("narrative", "")
        selftext = post.get("selftext") or ""

        content_hash = hashlib.sha256(
            f"{title}|{selftext}".encode(),
        ).hexdigest()

        signal_id = hashlib.sha256(
            f"reddit:{post_id}:{preference_tag}".encode(),
        ).hexdigest()[:64]

        post_date = None
        created_utc = post.get("created_utc", 0)
        if created_utc:
            post_date = datetime.fromtimestamp(
                created_utc, tz=timezone.utc,
            ).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Build raw thread text: post body + all comments (for future Pinecone embedding)
        raw_parts = [f"[POST] {title}", selftext] if selftext else [f"[POST] {title}"]
        for c in comments:
            raw_parts.append(
                f"[COMMENT score={c['score']} depth={c.get('depth',0)}] {c['body']}"
            )
        raw_thread_text = "\n\n".join(raw_parts)

        return {
            "signal_id": signal_id,
            "signal_source": "reddit",
            "source_native_id": post_id,
            "preference_tag": preference_tag,
            "title": title,
            "snippet_text": narrative,
            "raw_thread_text": raw_thread_text,
            "url": f"https://reddit.com{post.get('permalink', '')}",
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
                "discussion_date": post_date,
                "subreddit": post.get("subreddit", ""),
                "post_score": post.get("score", 0),
                "num_comments": post.get("num_comments", 0),
                "comments_analyzed": len(comments),
                "stage1_score": thread.get("_stage1_score"),
                "stage2_score": cls.get("relevance_score"),
                "thread_quality": cls.get("thread_quality", {}),
                "evidence": cls.get("evidence", []),
                "query_used": thread.get("_query", ""),
            }),
            "pipeline_run_id": self.pipeline_run_id,
        }

    # ── Staging + Merge ──────────────────────────────────────

    def _create_staging_table(self) -> str:
        batch_id = self.pipeline_run_id[:8]
        table = f"RAW.REDDIT_STAGING_{batch_id}"

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
        self.log.info("staging_table_created", table=table)
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
    pipeline = RedditPipeline()
    result = pipeline.run()
    raise SystemExit(0 if result.status == "success" else 1)
