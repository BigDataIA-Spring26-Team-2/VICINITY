"""Tests for app.services.pinecone_search."""

import pytest
from unittest.mock import patch, MagicMock
from app.services.pinecone_search import search_narratives, search_narratives_multi


def _mock_embedding():
    """Fake 1536-dim embedding."""
    return [0.01] * 1536


def _mock_pinecone_matches(n=3):
    """Fake Pinecone query response."""
    return {
        "matches": [
            {
                "id": f"sig_{i}",
                "score": 0.85 - (i * 0.05),
                "metadata": {
                    "signal_source": "crime" if i == 0 else "reddit",
                    "preference_tag": "safety",
                    "sentiment": "negative",
                    "neighborhoods": ["Allston"],
                    "category": "violence" if i == 0 else "general",
                    "url": f"https://source.com/{i}",
                },
            }
            for i in range(n)
        ]
    }


class TestSearchNarratives:

    @patch("app.services.pinecone_search._get_index")
    @patch("app.services.pinecone_search._embed")
    @patch("app.services.pinecone_search._generate_hypothetical")
    def test_hyde_flow(self, mock_hyde, mock_embed, mock_index):
        mock_hyde.return_value = "A violent assault occurred near Allston at 11 PM"
        mock_embed.return_value = _mock_embedding()
        mock_idx = MagicMock()
        mock_idx.query.return_value = _mock_pinecone_matches()
        mock_index.return_value = mock_idx

        result = search_narratives("is it safe in Allston at night?")
        assert result.success
        assert result.total_count == 3
        assert result.data[0]["source"] == "crime"
        # HyDE was used: embed was called with hypothetical, not original question
        mock_embed.assert_called_once_with(
            "A violent assault occurred near Allston at 11 PM"
        )

    @patch("app.services.pinecone_search._get_index")
    @patch("app.services.pinecone_search._embed")
    @patch("app.services.pinecone_search._generate_hypothetical")
    def test_skip_hyde(self, mock_hyde, mock_embed, mock_index):
        mock_embed.return_value = _mock_embedding()
        mock_idx = MagicMock()
        mock_idx.query.return_value = _mock_pinecone_matches()
        mock_index.return_value = mock_idx

        result = search_narratives("Allston crime", skip_hyde=True)
        assert result.success
        mock_hyde.assert_not_called()
        mock_embed.assert_called_once_with("Allston crime")

    @patch("app.services.pinecone_search._get_index")
    @patch("app.services.pinecone_search._embed")
    @patch("app.services.pinecone_search._generate_hypothetical")
    def test_hyde_failure_falls_back(self, mock_hyde, mock_embed, mock_index):
        mock_hyde.return_value = None  # HyDE failed
        mock_embed.return_value = _mock_embedding()
        mock_idx = MagicMock()
        mock_idx.query.return_value = _mock_pinecone_matches()
        mock_index.return_value = mock_idx

        result = search_narratives("is Allston safe?")
        assert result.success
        # Falls back to embedding the raw question
        mock_embed.assert_called_once_with("is Allston safe?")

    @patch("app.services.pinecone_search._get_index")
    @patch("app.services.pinecone_search._embed")
    @patch("app.services.pinecone_search._generate_hypothetical")
    def test_retry_on_empty(self, mock_hyde, mock_embed, mock_index):
        mock_hyde.return_value = "hypothetical"
        mock_embed.return_value = _mock_embedding()
        mock_idx = MagicMock()
        # First call empty, second call has results
        mock_idx.query.side_effect = [
            {"matches": []},
            _mock_pinecone_matches(2),
        ]
        mock_index.return_value = mock_idx

        result = search_narratives("obscure question")
        assert result.success
        assert result.total_count == 2
        assert len(result.warnings) > 0  # broadened warning
        assert mock_idx.query.call_count == 2

    @patch("app.services.pinecone_search._get_index")
    @patch("app.services.pinecone_search._embed")
    @patch("app.services.pinecone_search._generate_hypothetical")
    def test_all_retries_exhausted(self, mock_hyde, mock_embed, mock_index):
        mock_hyde.return_value = "hypothetical"
        mock_embed.return_value = _mock_embedding()
        mock_idx = MagicMock()
        mock_idx.query.return_value = {"matches": []}
        mock_index.return_value = mock_idx

        result = search_narratives("impossible query")
        assert result.success  # succeeds with empty data
        assert result.total_count == 0
        assert any("No matching" in w for w in result.warnings)

    @patch("app.services.pinecone_search._get_index")
    @patch("app.services.pinecone_search._embed")
    @patch("app.services.pinecone_search._generate_hypothetical")
    def test_filters_passed(self, mock_hyde, mock_embed, mock_index):
        mock_hyde.return_value = "hypothetical"
        mock_embed.return_value = _mock_embedding()
        mock_idx = MagicMock()
        mock_idx.query.return_value = _mock_pinecone_matches(1)
        mock_index.return_value = mock_idx

        result = search_narratives(
            "question",
            filters={"signal_source": "crime", "neighborhoods": ["Allston"]},
        )
        assert result.success
        call_kwargs = mock_idx.query.call_args
        pc_filter = call_kwargs.kwargs.get("filter") or call_kwargs[1].get("filter")
        assert pc_filter["signal_source"] == {"$eq": "crime"}
        assert pc_filter["neighborhoods"] == {"$in": ["Allston"]}

    @patch("app.services.pinecone_search._embed")
    def test_embedding_failure(self, mock_embed):
        mock_embed.side_effect = Exception("OpenAI rate limit")
        result = search_narratives("question", skip_hyde=True)
        assert not result.success
        assert "Embedding failed" in result.error


class TestSearchNarrativesMulti:

    @patch("app.services.pinecone_search.search_narratives")
    def test_deduplication(self, mock_search):
        # Two queries return overlapping results
        from app.services.listing_queries import QueryResult
        mock_search.side_effect = [
            QueryResult(success=True, query_type="search_narratives", data=[
                {"signal_id": "s1", "score": 0.9, "source": "crime"},
                {"signal_id": "s2", "score": 0.8, "source": "reddit"},
            ], total_count=2),
            QueryResult(success=True, query_type="search_narratives", data=[
                {"signal_id": "s2", "score": 0.85, "source": "reddit"},  # higher score
                {"signal_id": "s3", "score": 0.7, "source": "news"},
            ], total_count=2),
        ]

        result = search_narratives_multi(["query1", "query2"])
        assert result.success
        assert result.total_count == 3  # s1, s2, s3 deduplicated
        # s2 should keep higher score (0.85)
        s2 = next(d for d in result.data if d["signal_id"] == "s2")
        assert s2["score"] == 0.85

    @patch("app.services.pinecone_search.search_narratives")
    def test_max_queries_capped(self, mock_search):
        from app.services.listing_queries import QueryResult
        mock_search.return_value = QueryResult(
            success=True, query_type="search_narratives",
            data=[], total_count=0,
        )

        result = search_narratives_multi(
            ["q1", "q2", "q3", "q4", "q5", "q6"]  # 6 queries
        )
        assert result.success
        # max_queries from config is 3
        assert mock_search.call_count <= 3