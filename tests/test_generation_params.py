import io
import unittest

from PIL import Image

from generation_params import (
    detect_aspect_ratio_from_image,
    resolve_image_generation_params,
)


class GenerationParamsTest(unittest.TestCase):
    def test_prompt_ratio_and_resolution(self):
        params = resolve_image_generation_params(
            "请生成一张 16:9 小学数学 PPT 正文页成图，4K",
            default_aspect_ratio="4:3",
        )
        self.assertEqual(params["aspect_ratio"], "16:9")
        self.assertEqual(params["resolution"], "4K")
        self.assertEqual(params["size"], "3840x2160")

    def test_exact_dimensions_can_be_reversed(self):
        params = resolve_image_generation_params("输出 2336x3504")
        self.assertEqual(params["aspect_ratio"], "2:3")
        self.assertEqual(params["resolution"], "4K")

    def test_image_ratio_uses_nearest_common_ratio(self):
        output = io.BytesIO()
        Image.new("RGB", (1080, 1920)).save(output, "PNG")
        self.assertEqual(
            detect_aspect_ratio_from_image(output.getvalue(), "4:3"), "9:16"
        )

    def test_config_defaults_are_used_when_prompt_has_no_parameters(self):
        params = resolve_image_generation_params(
            "生成一张课程插图",
            default_resolution="2K",
            default_aspect_ratio="4:3",
        )
        self.assertEqual(params["size"], "2048x1536")

    def test_star_ratio_notation_is_supported(self):
        params = resolve_image_generation_params("小学课件正文页，4*3，1K")
        self.assertEqual(params["aspect_ratio"], "4:3")
        self.assertEqual(params["size"], "1024x768")


if __name__ == "__main__":
    unittest.main()
