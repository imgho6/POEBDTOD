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


    def test_news_is_short_and_never_ends_with_an_ellipsis(self):
        post = {
            "text": (
                "У китайському місті Шеньчжень розпочалися тристоронні перемовини "
                "за участі президента США Дональда Трампа, лідера Китаю Сі Цзіньпіна "
                "та очільника Кремля Володимира Путіна. "
                "Це перша зустріч такого формату за участі лідерів трьох держав. "
                "Перші заяви Трампа: Трамп заявив про скорочення ядерних озброєнь. "
                "Окрім цього, він сказав ще багато довгих речей. "
                "Путін говорив про енергетичну…"
            )
        }
        message = editorial.make_news_message(post)
        self.assertIsNotNone(message)
        self.assertLessEqual(len(message), 450)
        self.assertNotIn("…", message)
        self.assertNotIn("...", message)
        self.assertNotIn("Окрім цього", message)

    def test_rejects_geopolitical_story_with_only_a_late_energy_mention(self):
        text = (
            "У Шеньчжені розпочалися тристоронні перемовини за участі лідерів "
            "США, Китаю та Росії. Перші заяви стосувалися глобальної безпеки. "
            "Наприкінці зустрічі обговорили енергетичну безпеку."
        )
        self.assertFalse(editorial.relevant_post("ukrinform_news", text, "national"))

    def test_similar_leads_are_detected_as_the_same_story(self):
        first = editorial.make_news_lead(
            "На Полтавщині змінять графіки погодинних відключень електроенергії."
        )
        second = editorial.make_news_lead(
            "На Полтавщині змінять графіки погодинних відключень електроенергії!"
        )
        self.assertTrue(editorial.same_news_lead(first, second))

    def test_importance_score_prioritizes_urgent_local_outage_news(self):
        ordinary = "На Полтавщині діятимуть графіки погодинних відключень електроенергії."
        urgent = (
            "На Полтавщині через аварійне пошкодження мереж знеструмлено кілька громад. "
            "Енергетики відновлюють електропостачання."
        )
        ordinary_score = editorial.classify_news("suspilnepoltava", ordinary, "local")["importance"]
        urgent_score = editorial.classify_news("suspilnepoltava", urgent, "local")["importance"]
        self.assertGreater(urgent_score, ordinary_score)

    def test_detects_rephrased_story_with_different_word_forms(self):
        first = (
            "На Полтавщині через пошкодження електромереж знеструмлено кілька громад. "
            "Енергетики відновлюють живлення."
        )
        second = (
            "У кількох громадах Полтавщини відсутнє електропостачання через пошкоджені мережі, "
            "бригади повертають світло."
        )
        self.assertTrue(editorial.same_news_content(first, second))

    def test_incomplete_trailing_fragment_is_not_published(self):
        post = {
            "text": (
                "На Полтавщині змінять графіки відключень електроенергії. "
                "Рішення ухвалили після перевірки мереж. "
                "Деталі повідомлять згодом, енергетичну…"
            )
        }
        message = editorial.make_news_message(post)
        self.assertNotIn("енергетичну", message)


    def test_importance_and_relevance_scores_are_calculated(self):
        text = (
            "На Полтавщині аварійно знеструмлено тисячі споживачів через пошкодження електромереж. "
            "Енергетики відновлюють електропостачання."
        )
        result = editorial.classify_news("poltavskaoda", text, "local")
        self.assertTrue(result["relevant"])
        self.assertGreater(result["relevance"], 0)
        self.assertGreater(result["importance"], 40)

    def test_official_sources_have_higher_priority_than_commercial_media(self):
        priorities = {source[0]: source[3] for source in editorial.SOURCES}
        self.assertGreater(priorities["poltavskaoda"], priorities["tch_channel"])
        self.assertGreater(priorities["ukrinform_news"], priorities["tch_channel"])

    def test_semantically_similar_news_with_changed_headline_are_detected(self):
        first = (
            "На Полтавщині графіки погодинних відключень електроенергії змінять завтра "
            "після ремонту підстанції."
        )
        second = (
            "У Полтавській області завтра зміняться графіки погодинних відключень світла "
            "після ремонту підстанції."
        )
        self.assertTrue(editorial.same_news_content(first, second))


if __name__ == "__main__":
    unittest.main()
