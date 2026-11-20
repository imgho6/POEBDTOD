import importlib.util
from pathlib import Path
import unittest

MODULE_PATH = Path(__file__).with_name("telegram-admin.py")
SPEC = importlib.util.spec_from_file_location("telegram_admin", MODULE_PATH)
admin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(admin)


class ForwardedNewsCleanerTests(unittest.TestCase):
    def test_removes_standalone_handles_links_and_promotional_lines(self):
        source = (
            "На Полтавщині оновили графік відключень.\n"
            "\n"
            "@some_channel\n"
            "https://t.me/some_channel\n"
            "Підписуйтесь на наш канал\n"
            "Реклама: @ads_channel"
        )
        result = admin.clean_forwarded_news(source)
        self.assertIn("На Полтавщині оновили графік відключень.", result)
        self.assertNotIn("@some_channel", result)
        self.assertNotIn("t.me/", result)
        self.assertNotIn("Підписуйтесь", result)
        self.assertNotIn("Реклама", result)
        self.assertTrue(result.endswith("⚡ Світло.ua"))

    def test_removes_inline_channel_watermark_but_keeps_news_text(self):
        result = admin.clean_forwarded_news(
            "На Полтавщині оновили графік відключень. | @some_channel"
        )
        self.assertIn("На Полтавщині оновили графік відключень.", result)
        self.assertNotIn("@some_channel", result)

    def test_does_not_remove_normal_news_mentions(self):
        result = admin.clean_forwarded_news(
            "У повідомленні @energy_service пояснили причину змін."
        )
        self.assertIn("@energy_service", result)


if __name__ == "__main__":
    unittest.main()
