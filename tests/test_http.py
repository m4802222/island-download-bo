import unittest
from unittest.mock import Mock, patch

from islandbot.http import fetch


class HttpTransportTests(unittest.TestCase):
    @patch("islandbot.http.urllib.request.urlopen")
    def test_default_transport_uses_urlopen(self, urlopen):
        response = Mock(status=200, headers={})
        response.read.return_value = b'{"ok": true}'
        urlopen.return_value = response

        result = fetch("https://example.invalid/api", form={"offset": 0})

        self.assertEqual(result.status, 200)
        self.assertEqual(result.json(), {"ok": True})
        urlopen.assert_called_once()


if __name__ == "__main__":
    unittest.main()
