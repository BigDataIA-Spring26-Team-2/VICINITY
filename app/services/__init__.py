"""Vicinity services — agent-facing data access and mutation layer.

All service functions take a Snowflake cursor + typed parameters,
return structured results. No HTTP, no agent state, no LangGraph
dependency. Services are callable from agents, routers, and tests.

Modules:
    listing_queries    — search, detail, compare, scorecard history
    crime_queries      — single-point and corridor ad-hoc queries
    complaint_queries  — single-point 311 queries
    user_data          — profiles, bookmarks, conversations CRUD
    pinecone_search    — HyDE retrieval from Pinecone
    amenity_lookup     — live Overpass/Google Places preference queries
    sql_freeform       — schema-injected freeform SELECT
    url_health         — URL validation and flagging
"""