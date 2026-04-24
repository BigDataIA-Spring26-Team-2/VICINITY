# Vicinity — Boston Housing Intelligence

A multi-agent system that helps people find safe, livable apartments in Boston by combining twelve public data sources — crime records, 311 complaints, Citizen incidents, MBTA transit, OSM amenities, Reddit discussions, Google News, and more — into scored, personalized, temporally-tracked recommendations.

| Resource | Link |
|---|---|
| Live Demo | |
| Docs | |
| Airflow UI | |
| Video | |
| Codelabs | |

**DAMG 7245 — Big Data and Intelligent Analytics**

| Member | Contribution |
|---|---|
| Anirudh Raj | 33.3% |
| Minal Naranje | 33.3% |
| Janhavi Patil | 33.3% |

**Attestation:** WE ATTEST THAT WE HAVEN'T USED ANY OTHER STUDENTS' WORK IN OUR ASSIGNMENT AND ABIDE BY THE POLICIES LISTED IN THE STUDENT HANDBOOK.

**AI usage:** Claude Opus 4.6 alongside GitHub Copilot — used for agent graph design, prompt engineering, pipeline orchestration patterns, test scaffolding, and documentation.

---

## What it does

Vicinity answers *where should I live in Boston* with accumulated evidence, not a single snapshot.

- **Search** — finds apartments matching budget, bedrooms, neighborhood, and lifestyle preferences; ranks by pre-computed safety, livability, and transit scores.
- **Monitor** — bookmark up to 20 listings with a configurable watch period (up to 30 days); nightly Airflow DAGs accumulate daily scorecards per listing.
- **Ask** — natural-language questions answered via dual retrieval: SQL against Snowflake for structured data (crime counts, trends, amenities), semantic search against Pinecone for narrative evidence (Reddit discussions, news coverage, incident descriptions).
- **Compare** — when the watch period ends, a report synthesizes the accumulated evidence across bookmarks, weighs tradeoffs, and picks a winner with justification.
- **Configure** — save profile, add commute routes scored for safety along the actual path at the hours traveled, flag broken URLs, trigger tracking for new topics.

---

## System Architecture

![Infrastructure](./images/arch.png)

Single-VM deployment on **GCP Compute Engine** running four containers:

- **FastAPI + MCP server** — REST API on port 8000, MCP streamable-HTTP on port 8001, serves the agent graph and listing routers.
- **React frontend** — port 80, served by nginx, consumes the SSE event stream from the chat API.
- **Redis** — cache layer for geocoding, Overpass amenity lookups, commute route computations.
- **Airflow** — nightly master DAG orchestrating 10 ingestion pipelines, embedding sync, and scorecard scoring.

**Data plane:** Snowflake for structured records (listings, crime, 311, scorecards, user data), Pinecone for narrative vectors with HyDE-enhanced retrieval, AWS S3 for raw API backup.

---

## Agent Architecture

Built on **LangGraph StateGraph** with MemorySaver checkpointer. Every user turn enters through the input gate, routes to one of five paths, and exits through a guardrail.

### High-level flow

![Agent Flow](./images/agent_diagram.png)

An **intent router** classifies the user's message into one of five routes. Read-only questions go straight to the **Chat Agent**. Search and report requests run through specialized sub-agents whose output flows back through the chat agent for synthesis. Write operations go through the **Organizer**, which always asks before committing. The **Safety Check** scrubs PII, truncates long responses, and retries on empty output before the response leaves the system.

*Why this shape:* one classifier, one exit guardrail, and specialized agents in between. Failures are bounded to a single node; state corruption is impossible because every state field has a single writer per path.

### Detailed graph

![LangGraph Topology](./images/langraph.png)

Every node corresponds to an `add_node` call in `app/agents/graph.py`; every edge corresponds to an `add_edge` or `add_conditional_edges` branch.

**Core design decisions:**

- **Input gate classifies every turn.** Uses a structured-JSON prompt with six valid routes (`chat`, `search`, `report`, `organizer`, `confirm`, `block`). Sees the last three exchanges as disambiguation context; scrubs PII before the LLM ever sees user input.
- **ReAct loops are tool-bounded.** Every agent runs a LLM ↔ tools loop with a hard cap (`max_calls_per_turn: 15`). When the limit hits, orphan `tool_calls` are stripped by `sanitize_messages` on the next LLM input — no separate patching step needed.
- **Sub-agents funnel through chat_react for synthesis.** Accepted trade-off: a single place owns user-facing formatting, at the cost of prompt discipline on the chat agent. The alternative (each sub-agent emits final text) was implemented earlier and reverted after it introduced HumanMessage echo bugs.
- **HITL writes are split into plan + confirm.** `organizer_plan` proposes the write and sets `pending_confirmation`; `organizer_confirm` calls `interrupt()` and pauses. On resume: *approve* → execute, *reject* → acknowledge, *modify* → re-plan with the user's instruction. Never executes without explicit consent.
- **Guardrail is the single exit.** Runs PII scrub → tool-health check → empty-retry (up to 2 attempts) → length truncation. Consistent safety envelope regardless of which agent produced the response.

---

## Data Pipeline

Airflow master DAG (`dags/vicinity_master.py`) orchestrates 3 phases, scheduled daily at **06:00 UTC**, with `catchup=False`:

1. **Ingest (10 tasks, parallel)** — crime, 311, Citizen, MLS listings (+ Craigslist fallback), Reddit (livability + lifestyle), Google News (livability + lifestyle), Eventbrite. Each pipeline is idempotent via content-hash dedup.
2. **Sync** — embed new lifestyle signals into Pinecone (short-circuits if nothing new).
3. **Score** — compute per-listing percentile scores across safety, livability, transit; write daily scorecards to `SCORECARDS.LOCATION_SCORECARD` and update `SCORECARDS.LISTING_SUMMARY`.

**Gate after phase 1:** requires ≥5 successful tasks AND `ingest_listings` must succeed (listings are load-bearing). On failure, phase 2 and 3 don't run.

**Scoring methodology:**
- Percentile ranking across all active listings — lower-is-better for crime and complaints, higher-is-better for amenities and transit.
- Two-tier spatial join: exact `ST_DISTANCE` where coordinates exist, neighborhood/zip fallback otherwise.
- Weighted complaint score: quality-of-life complaints (noise, pest, heat, housing) × 1.0 + infrastructure complaints × 0.3.
- Confidence values based on data density — low confidence flags a possible coverage gap, not necessarily bad conditions.
- Full provenance: every scorecard row stores the exact config (radii, windows, weights) used to compute it.

**Slack notifications** fire on pipeline start, retry, SLA miss, and completion. Failed-task summary includes direct links to logs. Configured via `SLACK_WEBHOOK_URL` in `.env`; gracefully no-ops if absent.

---

## Project Structure

```
vicinity/
├── app/
│   ├── agents/              # LangGraph: chat, search, report, organizer, guardrails
│   │   ├── graph.py         # StateGraph assembly — single source of truth for topology
│   │   ├── tools/           # read_tools, search_tools, write_tools
│   │   └── ...
│   ├── pipelines/           # Ingestion pipelines (crime, 311, listings, reddit, news, ...)
│   ├── scoring/             # Percentile ranking, confidence, YoY, route corridor scoring
│   ├── routers/             # FastAPI: chat, listings, users, health
│   ├── services/            # Snowflake query services, user data, URL health
│   └── core/                # Cache, config loader, base pipeline, auth
├── mcp_vicinity/            # MCP server (streamable-HTTP transport)
├── airflow/
│   ├── dags/                # vicinity_master + per-pipeline configs
│   └── dag_utils.py         # Slack hooks, shared callbacks
├── config/                  # agents.yml, scoring.yml, dags.yml, sources/
├── docker/                  # Dockerfile.api, Dockerfile.frontend, docker-compose.yml
├── frontend/                # React app
├── alembic/                 # Snowflake schema migrations
├── scripts/                 # chat.py (terminal interface), utilities
├── tests/
│   ├── unit/                # agents, routers, scoring (Hypothesis properties)
│   └── integration/         # graph routing, HITL, sub-agent synthesis, sanitization
├── images/                  # Architecture diagrams
├── Makefile                 # Build, test, deploy — one source of truth
├── requirements.txt         # Full deps (pipelines + app)
├── requirements-api.txt     # Trimmed API-only deps
├── .env.example
└── deploy.env.example       # GCP project, region, instance, zone
```

---

## Setup & Deployment

Python 3.12 required. GCP project with Compute Engine, Artifact Registry, and gcloud CLI configured.

### 1. Clone and configure

```bash
git clone https://github.com/<org>/vicinity.git
cd vicinity
cp .env.example .env              # fill in Snowflake, API keys, Pinecone, etc.
cp deploy.env.example deploy.env  # fill in GCP project, region, instance
```

See `.env.example` for the full variable list. Required: Snowflake credentials, `DEEPSEEK_API_KEY`, `OPENAI_API_KEY`, `PINECONE_API_KEY`, `GOOGLE_MAPS_API`, `JWT_SECRET`. Optional: `SLACK_WEBHOOK_URL`, `AWS_*` for raw-response backup.

### 2. Local development

```bash
make up        # Redis + API + Frontend + MCP via docker-compose
make health    # verify all four services are up
make logs      # tail logs
make down      # stop
```

Local endpoints: Frontend `:3000`, API `:8000/docs`, MCP `:8001/mcp`, Redis `:6379`.

### 3. Test

```bash
make test      # unit tests (pytest)
make lint      # ruff check --fix
make ci        # lint → test → build (all images)
```

Test layout:
- **Unit** (`tests/unit/`) — routers, services, agents; property-based tests for the scoring module (Hypothesis).
- **Integration** (`tests/integration/`) — live graph routing, HITL approve/reject/modify, sub-agent→chat synthesis, message sanitization against poisoned state. Write tools are sentinel-intercepted so no row ever reaches Snowflake during tests.

### 4. Deploy to GCP

```bash
make all       # build → push → upload → deploy API+Redis+FE+MCP → deploy Airflow → status
```

Idempotent. Creates the VM if missing, opens firewall rules, creates the Artifact Registry repo, promotes the VM's IP to static. Safe to re-run.

**Incremental redeploys:**
```bash
make redeploy      # rebuild + push + redeploy API (keeps Redis/Airflow running)
make redeploy-fe   # rebuild + push + redeploy Frontend only
make deploy-af     # redeploy Airflow only
```

**Operations:**
```bash
make status        # live health of all services on the VM
make ssh           # SSH into the VM
make expose-af     # open Airflow UI (port 8081)
make hide-af       # close it again
```

Airflow UI access is off by default. Enable only when needed.

### 5. Daily commands

| Command | When to run |
|---|---|
| `make up` | Local dev, every morning |
| `make test` | Before every commit |
| `make redeploy` | Push an API change to prod |
| `make status` | Verify prod health |
| `make all` | Full clean deploy from scratch |

---

## Observability

- **Structured logs** — every component binds `trace_id`, `session_id`, and `pipeline_run_id` via `structlog`. One grep follows a request end-to-end across the agent graph, tools, and downstream services.
- **Per-turn cost tracking** — chat turns write a row to `RAW.LLM_USAGE_LOG` (model, tokens, duration, cost) with `source='chat'`, sharing the same schema as pipeline runs.
- **Airflow Slack** — pipeline start + completion summary, with failed-task breakdown and direct log links. Falls silent if no webhook configured.
- **Health endpoint** — `/healthz` verifies Snowflake, Redis, and Pinecone connectivity.

---

## Key Technologies

| Layer | Stack |
|---|---|
| Agents | LangGraph, LiteLLM (DeepSeek primary, GPT-4o fallback) |
| API | FastAPI, Starlette SSE, MCP (streamable-HTTP) |
| Data | Snowflake, Pinecone, Redis, AWS S3 |
| Orchestration | Airflow (CeleryExecutor), Docker Compose |
| Frontend | React, nginx |
| Cloud | GCP Compute Engine, Artifact Registry |
| Testing | pytest, pytest-asyncio, Hypothesis |

---

## License

See `LICENSE`.