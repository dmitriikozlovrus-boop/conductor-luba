import unittest
from email.message import Message
from io import BytesIO
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from conductor.http import request_json


class HttpRetryTest(unittest.TestCase):
    def test_request_json_retries_notion_rate_limit(self):
        headers = Message()
        headers["Retry-After"] = "0.1"
        rate_limit = HTTPError(
            "https://api.notion.com/v1/pages/page",
            429,
            "rate limited",
            headers,
            BytesIO(b'{"message":"rate limited"}'),
        )
        success = Mock()
        success.__enter__ = Mock(return_value=Mock(read=Mock(return_value=b'{"ok":true}')))
        success.__exit__ = Mock(return_value=False)
        with patch("conductor.http.request.urlopen", side_effect=[rate_limit, success]) as urlopen:
            with patch("conductor.http.time.sleep") as sleep:
                result = request_json("GET", "https://api.notion.com/v1/pages/page")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(0.1)


if __name__ == "__main__":
    unittest.main()
