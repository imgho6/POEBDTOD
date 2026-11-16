import importlib.util
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("telegram-editorial.py")
SPEC = importlib.util.spec_from_file_location("telegram_editorial", MODULE_PATH)
editorial = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(editorial)


class EditorialFilterTests(unittest.TestCase):
    def test_rejects_irrelevant_police_news_from_other_regions(self):
        text = "Поліцейські автомобілі «Фантоми» патрулюватимуть дороги Львівської та Одеської областей"
        self.assertFalse(editorial.relevant_post("ukrinform_news", text, "national"))

    def test_rejects_general_transport_news_even_if_poltava_is_mentioned(self):
        text = "На Полтавщині ремонтують дороги та змінюють маршрути автобусів"
        self.assertFalse(editorial.relevant_post("suspilnepoltava", text, "local"))

    def test_accepts_local_outage_information(self):
        text = "На Полтавщині діятимуть графіки погодинних відключень електроенергії"
        self.assertTrue(editorial.relevant_post("suspilnepoltava", text, "local"))

    def test_accepts_nationwide_electricity_information(self):
        text = "В Україні змінять графіки відключень електроенергії для споживачів"
        self.assertTrue(editorial.relevant_post("ukrinform_news", text, "national"))

    def test_rejects_non_electricity_national_news(self):
        text = "В Україні з 16 листопада патрулюватимуть дороги нові автомобілі"
        self.assertFalse(editorial.relevant_post("ukrinform_news", text, "national"))

    def test_news_message_does_not_show_source_link(self):
        post = {
            "text": "На Полтавщині змінять графіки відключень.",
            "handle": "suspilnepoltava",
            "post": "suspilnepoltava/12345",
        }
        message = editorial.make_news_message(post)
        self.assertIn("графіки відключень", message)
        self.assertNotIn("Джерело", message)
        self.assertNotIn("t.me/suspilnepoltava", message)
        self.assertNotIn("href=", message)
        self.assertNotIn("sendPhoto", message)


    def test_news_message_contains_only_the_news_text(self):
        post = {
            "text": "На Полтавщині змінять графіки відключень електроенергії.",
            "handle": "suspilnepoltava",
            "post": "suspilnepoltava/123",
        }
        message = editorial.make_news_message(post)
        self.assertIsInstance(message, str)
        self.assertIn("графіки відключень", message)
        self.assertNotIn("Джерело", message)
        self.assertNotIn("href=", message)
        self.assertNotIn("<img", message)
        self.assertFalse(hasattr(editorial, "send_photo"))
        self.assertFalse(hasattr(editorial, "fetch_image"))

    def test_caption_drops_source_attribution_and_subscription_tail(self):
        post = {"text": "На Полтавщині змінять графіки відключень.\nІнформацію повідомили енергетики.\nПідпишись на Укрінформ | Facebook | YouTube"}
        caption = editorial.make_caption(post)
        self.assertIn("графіки відключень", caption)
        self.assertNotIn("Джерело:", caption)
        self.assertIn("Інформацію повідомили енергетики", caption)
        self.assertNotIn("Підпишись", caption)
        self.assertNotIn("Facebook", caption)


if __name__ == "__main__":
    unittest.main()
