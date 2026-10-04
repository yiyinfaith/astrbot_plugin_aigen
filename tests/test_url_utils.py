import unittest

from utils import match_keyword_in_text, normalize_api_root


class UrlNormalizationTest(unittest.TestCase):
    def test_version_suffix_is_removed(self):
        self.assertEqual(
            normalize_api_root("https://api.example.com/v1beta"),
            "https://api.example.com",
        )

    def test_openai_endpoint_is_reduced_to_prefixed_root(self):
        self.assertEqual(
            normalize_api_root("https://api.example.com/openai/v1/chat/completions"),
            "https://api.example.com/openai",
        )

    def test_gemini_endpoint_is_reduced_to_prefixed_root(self):
        self.assertEqual(
            normalize_api_root(
                "https://api.example.com/google/v1beta/models/gemini-2.5-flash-image:generateContent?key=old"
            ),
            "https://api.example.com/google",
        )

    def test_image_endpoint_and_query_are_removed(self):
        self.assertEqual(
            normalize_api_root(
                "https://api.example.com/api/v1/images/generations?foo=bar"
            ),
            "https://api.example.com/api",
        )

    def test_response_endpoint_is_reduced_to_root(self):
        self.assertEqual(
            normalize_api_root("https://api.example.com/openai/v1/response"),
            "https://api.example.com/openai",
        )


class PresetKeywordMatchTest(unittest.TestCase):
    def test_keyword_can_follow_a_mention(self):
        self.assertEqual(
            match_keyword_in_text("@小明手办化", ["手办化", "手办化2"]),
            ("手办化", 3),
        )

    def test_longest_overlapping_keyword_wins(self):
        self.assertEqual(
            match_keyword_in_text("请手办化2", ["手办化", "手办化2"]),
            ("手办化2", 1),
        )

    def test_empty_keywords_are_ignored(self):
        self.assertIsNone(match_keyword_in_text("手办化", ["", "  "]))


if __name__ == "__main__":
    unittest.main()
