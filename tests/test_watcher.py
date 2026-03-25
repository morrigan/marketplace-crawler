import unittest
from unittest.mock import patch

from watcher import extract_candidates, filter_candidates, run_marketplace, update_seen_items


class WatcherTests(unittest.TestCase):
    def test_extract_candidates_from_anchor_tags(self) -> None:
        html = """
        <html>
          <body>
            <a href="/listing/123">Vintage Camera Canon AE-1</a>
            <a href="/about">About</a>
          </body>
        </html>
        """

        candidates = extract_candidates(html, "https://example.com/search?q=camera", None)

        self.assertEqual(candidates[0]["url"], "https://example.com/listing/123")
        self.assertEqual(candidates[0]["title"], "Vintage Camera Canon AE-1")

    def test_filter_candidates_with_keywords(self) -> None:
        candidates = [
            {
                "url": "https://example.com/listing/123",
                "title": "Vintage Camera Canon AE-1",
            },
            {
                "url": "https://example.com/listing/456",
                "title": "Tripod",
            },
        ]

        marketplace = {
            "keywords": ["canon", "camera"],
            "match_mode": "all",
            "allowed_domains": ["example.com"],
            "candidate_url_patterns": ["/listing/"],
        }

        results = filter_candidates(candidates, marketplace, "https://example.com/search?q=camera")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://example.com/listing/123")

    def test_blocked_marker_raises_error(self) -> None:
        marketplace = {
            "name": "Blocked Marketplace",
            "search_url": "https://example.com/search?q=test",
            "allowed_domains": ["example.com"],
            "candidate_url_patterns": ["/listing/"],
            "blocked_markers": ["captcha"],
        }

        with patch("watcher.fetch_html", return_value="<html>captcha</html>"):
            with self.assertRaisesRegex(ValueError, "blocked"):
                run_marketplace(marketplace, {}, set(), {"timeout_seconds": 30}, False)

    def test_global_seen_items_prevent_duplicate_notification_across_searches(self) -> None:
        marketplace = {
            "name": "Marketplace",
            "search_url": "https://example.com/search?q=test",
            "allowed_domains": ["example.com"],
            "candidate_url_patterns": ["/listing/"],
            "keywords": ["camera"],
            "match_mode": "any",
            "bootstrap_existing": False,
        }
        html = '<a href="/listing/123">Camera Listing</a>'
        first_seen_items = {}
        global_items = {}

        with patch("watcher.fetch_html", return_value=html):
            first_result = run_marketplace(marketplace, first_seen_items, set(), {"timeout_seconds": 30}, False)

        self.assertEqual(len(first_result.new_items), 1)
        update_seen_items(first_seen_items, global_items, first_result)

        with patch("watcher.fetch_html", return_value=html):
            second_result = run_marketplace(marketplace, {}, set(global_items), {"timeout_seconds": 30}, False)

        self.assertEqual(len(second_result.new_items), 0)


if __name__ == "__main__":
    unittest.main()
