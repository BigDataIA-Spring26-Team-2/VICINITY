"""Scoring package — nightly batch scoring for all active listings.

Computes three dimensions (safety, livability, transit) as percentile
ranks across all active listings. Stores raw metrics + historical
series in scoring_metadata VARIANT for agent transparency.

Lifestyle dimension is per-user, computed at report time via Pinecone.
"""