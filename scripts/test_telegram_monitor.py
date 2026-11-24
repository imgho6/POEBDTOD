import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

MODULE_PATH = Path(__file__).with_name("telegram-monitor.py")
SPEC = importlib.util.spec_from_file_location("telegram_monitor", MODULE_PATH)
monitor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(monitor)


class FakeResponse:
    def __init__(self, body=b"<html><body>Page is available</body></html>", status=200,
                 content_type="text/html; charset=utf-8", url="https://prosvitlo.pp.ua/poltava"):
        self.body = body
        self.status = status
        self.headers = {"Content-Type": content_type}
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=-1):
        return self.body[:size]

    def geturl(self):
        return self.url


class WebsiteMonitorTests(unittest.TestCase):
    def test_http_200_is_healthy_without_exact_brand_text(self):
        with patch.object(monitor.urllib.request, "urlopen", return_value=FakeResponse()) as mocked:
            status, details = monitor.check_site()

        self.assertEqual(status, "ok")
        self.assertEqual(details["httpStatus"], 200)
        self.assertEqual(mocked.call_args.args[0].full_url, "https://prosvitlo.pp.ua/poltava")

    def test_empty_html_response_is_an_error(self):
        with patch.object(monitor.urllib.request, "urlopen", return_value=FakeResponse(body=b"")):
            status, details = monitor.check_site()

        self.assertEqual(status, "error")
        self.assertIn("порожню", details["reason"])

    def test_non_html_response_is_an_error(self):
        response = FakeResponse(content_type="application/json")
        with patch.object(monitor.urllib.request, "urlopen", return_value=response):
            status, details = monitor.check_site()

        self.assertEqual(status, "error")
        self.assertIn("не у форматі HTML", details["reason"])

    def test_http_failure_is_reported_as_failure(self):
        error = HTTPError(
            "https://prosvitlo.pp.ua/poltava", 503, "Service Unavailable", {}, None
        )
        with patch.object(monitor.urllib.request, "urlopen", side_effect=error):
            status, details = monitor.check_site()

        self.assertEqual(status, "error")
        self.assertIn("503", details["reason"])


if __name__ == "__main__":
    unittest.main()
