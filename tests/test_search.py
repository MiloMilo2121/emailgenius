import unittest
from unittest.mock import patch

from emailgenius.search import (
    build_company_news_queries,
    build_news_query,
    build_site_query,
    is_event_news_hit,
    search_web,
    select_official_site,
)
from emailgenius.types import SearchHit


class SearchTests(unittest.TestCase):
    def test_build_queries(self) -> None:
        self.assertEqual(build_site_query("Acme", "Vicenza"), "Acme Vicenza sito ufficiale")
        self.assertEqual(build_news_query("Acme", "Vicenza"), "Acme Vicenza news")

    def test_build_company_news_queries_adds_event_and_official_site_queries(self) -> None:
        queries = build_company_news_queries("Acme", "Vicenza", official_domain="acme.it")
        self.assertGreaterEqual(len(queries), 4)
        self.assertIn("investimenti fiere partnership", queries[0])
        self.assertTrue(any(query.startswith("site:acme.it") for query in queries))

    @patch.dict("os.environ", {"TAVILY_API_KEY": "test-key"}, clear=False)
    @patch("emailgenius.search.TavilyClient")
    def test_search_web_uses_tavily_and_maps_hits(self, mock_tavily_client) -> None:
        mock_client = mock_tavily_client.return_value
        mock_client.search.return_value = {
            "results": [
                {
                    "title": "Acme S.p.A. - Sito Ufficiale",
                    "url": "https://www.acme.it",
                    "content": "Produzione meccanica per automazione industriale",
                }
            ]
        }

        hits = search_web("Acme Vicenza sito ufficiale", max_results=5)

        mock_tavily_client.assert_called_once_with(api_key="test-key")
        mock_client.search.assert_called_once_with(
            query="Acme Vicenza sito ufficiale",
            max_results=5,
            search_depth="basic",
        )
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0], SearchHit(title="Acme S.p.A. - Sito Ufficiale", url="https://www.acme.it", snippet="Produzione meccanica per automazione industriale"))

    def test_is_event_news_hit_detects_event_keywords(self) -> None:
        event_hit = SearchHit(title="Acme investe in un nuovo impianto", url="https://news.example.com/acme-impianto")
        neutral_hit = SearchHit(title="Acme aggiorna il sito corporate", url="https://news.example.com/acme-sito")
        self.assertTrue(is_event_news_hit(event_hit))
        self.assertFalse(is_event_news_hit(neutral_hit))

    def test_select_official_site_prefers_company_domain(self) -> None:
        candidates = [
            SearchHit(title="ACME | LinkedIn", url="https://www.linkedin.com/company/acme"),
            SearchHit(title="Acme S.p.A. - Sito Ufficiale", url="https://www.acme.it"),
            SearchHit(title="News Acme", url="https://news.example.org/acme"),
        ]

        selected = select_official_site("Acme", "Vicenza", candidates)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.url, "https://www.acme.it/")


if __name__ == "__main__":
    unittest.main()
