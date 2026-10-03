import unittest

from utils import normalize_api_root, normalize_model_list


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


class ModelListNormalizationTest(unittest.TestCase):
    def test_old_and_new_model_entries_are_supported(self):
        self.assertEqual(
            normalize_model_list(
                [
                    "model-a",
                    {"id": "model-b"},
                    {"model": "model-c"},
                    {"name": "model-d"},
                    {},
                    None,
                    "model-a",
                ]
            ),
            ["model-a", "model-b", "model-c", "model-d"],
        )


if __name__ == "__main__":
    unittest.main()
