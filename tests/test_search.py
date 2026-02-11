import unittest

from emailgenius.search import (
    build_news_query,
    build_site_query,
    parse_bing_html,
    parse_bing_news_html,
    parse_duckduckgo_html,
    select_official_site,
)
from emailgenius.types import SearchHit


class SearchTests(unittest.TestCase):
    def test_build_queries(self) -> None:
        self.assertEqual(build_site_query("Acme", "Vicenza"), "Acme Vicenza sito ufficiale")
        self.assertEqual(build_news_query("Acme", "Vicenza"), "Acme Vicenza news")

    def test_parse_duckduckgo_results(self) -> None:
        html = """
        <html><body>
          <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.acme.it">Acme S.p.A.</a>
          <a class="result__a" href="https://www.linkedin.com/company/acme">Acme LinkedIn</a>
        </body></html>
        """
        hits = parse_duckduckgo_html(html, max_results=5)

        self.assertEqual(len(hits), 2)
        self.assertEqual(hits[0].url, "https://www.acme.it")
        self.assertEqual(hits[0].title, "Acme S.p.A.")

    def test_parse_bing_results(self) -> None:
        html = """
        <html><body>
          <li class="b_algo">
            <h2><a href="https://www.bing.com/ck/a?u=a1aHR0cHM6Ly93d3cuYWNtZS5pdA">Acme S.p.A. - Sito Ufficiale</a></h2>
          </li>
          <li class="b_algo">
            <h2><a href="https://www.bing.com/ck/a?u=a1aHR0cHM6Ly93d3cubGlua2VkaW4uY29tL2NvbXBhbnkvYWNtZQ">Acme | LinkedIn</a></h2>
          </li>
        </body></html>
        """
        hits = parse_bing_html(html, max_results=5)
        self.assertEqual(len(hits), 2)
        self.assertEqual(hits[0].url, "https://www.acme.it")
        self.assertEqual(hits[0].title, "Acme S.p.A. - Sito Ufficiale")

    def test_parse_bing_news_results(self) -> None:
        html = """
        <html><body>
          <a target="_blank" class="title" href="https://news.example.com/acme-ricavi"><h2>Acme ricavi record nel 2026</h2></a>
          <a target="_blank" class="title" href="https://another.example.com/acme-efficienza"><h2>Acme investe in efficienza energetica</h2></a>
        </body></html>
        """
        hits = parse_bing_news_html(html, max_results=5)
        self.assertEqual(len(hits), 2)
        self.assertEqual(hits[0].url, "https://news.example.com/acme-ricavi")
        self.assertEqual(hits[0].title, "Acme ricavi record nel 2026")

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
