"""搜索词清理与严格匹配，避免把本地版本号当成商店标题。"""
import unittest
from unittest.mock import Mock, patch

import scraper


class SearchTitleTests(unittest.TestCase):
    def test_explicit_trailing_versions_are_removed(self):
        for name in ("Summer Memories_v1.20260503b", "Summer Memories v1.2.3",
                     "Summer Memories-Ver.1.2 beta2", "Summer Memories version 1.2.3",
                     "Summer Memories【v1.2.3】", "Summer Memories (Version 1.2)",
                     "Summer Memories_v1.2.3-PC", "Summer Memories_版本：1.2.3",
                     "サマー・メモリーズ！_v1.20260503b"):
            with self.subTest(name=name):
                expected = "サマー・メモリーズ！" if name.startswith("サマー") else "Summer Memories"
                self.assertEqual(scraper.search_title(name), expected)

    def test_title_numbers_and_internal_versions_are_preserved(self):
        for name in ("Summer Memories 2", "Summer Memories 1.23", "Summer Memories 2026",
                     "Summer Memories v1.2 Deluxe", "Summer Memories Verbatim", "v1.2.3",
                     "Summer Memories (1.2)", "Summer Memories 2: A New Start"):
            with self.subTest(name=name):
                self.assertEqual(scraper.search_title(name), name)

    def test_search_cleans_query_for_all_sources(self):
        sources = {"steam": Mock(return_value=[]), "dlsite": Mock(return_value=[])}
        with patch.dict(scraper.SEARCHERS, sources, clear=True):
            self.assertEqual(scraper.search("Summer Memories_v1.20260503b"), ([], []))
        for source in sources.values():
            source.assert_called_once_with("Summer Memories")

    def test_links_are_recognized_before_removing_suffixes(self):
        url = "https://example-studio.itch.io/summer-memories-v1-2"
        with patch.object(scraper, "lookup_link", return_value=([], [])) as lookup, \
             patch.object(scraper, "search_title", side_effect=AssertionError("官网链接不能按文件名清理")):
            scraper.search(url)
        lookup.assert_called_once_with("itch", url)

    def test_exact_match_ignores_local_version_but_rejects_other_games(self):
        result = {"source":"dlsite", "id":"RJ12345678", "title":"Summer Memories 2", "alt":""}
        self.assertIs(scraper.exact_match("Summer Memories 2_v1.20260503b", [result]), result)
        self.assertIsNone(scraper.exact_match("Summer Memories_v1.20260503b", [result]))
        self.assertIsNone(scraper.exact_match("Summer", [result]))

    def test_exact_match_retains_source_priority_and_punctuation_rules(self):
        results = [{"source":source, "id":"example", "title":"サマー・メモリーズ!", "alt":""}
                   for source in ("dlsite", "steam")]
        self.assertEqual(scraper.exact_match("サマー・メモリーズ！_v1.2.3", results)["source"], "steam")

    def test_automatic_scrape_uses_clean_title_and_accepts_official_match(self):
        game = {"id":1, "name":"Summer Memories_v1.20260503b", "missing":0, "is_wishlist":0,
                "meta_source":"", "meta_id":"", "developer":"", "scrape_status":"unmatched"}
        local = {"name":game["name"], "developer":"", "hero":None, "logo":None}
        hit = {"source":"dlsite", "id":"RJ12345678", "title":"Summer Memories", "alt":""}
        details = Mock(return_value={**hit})
        with patch.object(scraper.library, "get_game", return_value=game), \
             patch.object(scraper, "local_for", return_value=(local, None)), \
             patch.object(scraper, "find_dlsite_code", return_value=None), \
             patch.object(scraper, "search", return_value=([hit], [])) as search, \
             patch.dict(scraper.DETAILS, {"dlsite":details}), \
             patch.object(scraper, "apply", return_value={"game_id":1}) as apply:
            message, reachable = scraper.auto_scrape(1)
        search.assert_called_once_with("Summer Memories", developer="")
        details.assert_called_once_with("RJ12345678")
        self.assertEqual(apply.call_args.args[1]["title"], "Summer Memories")
        self.assertTrue(reachable)
        self.assertIn("DLsite", message)
