import unittest
from unittest.mock import patch

from islandbot.clients import QBitClient
from islandbot.http import Response


class QBitClientTests(unittest.TestCase):
    def test_pause_uses_qbittorrent_v5_stop_endpoint(self):
        client = QBitClient("http://qbit:8080", "admin", "secret")
        client.cookie = "SID=test"
        with patch(
            "islandbot.clients.fetch",
            return_value=Response(200, b"", {}),
        ) as mocked:
            client.action("pause", "abc")
        self.assertIn("/api/v2/torrents/stop", mocked.call_args.args[0])
        self.assertEqual(mocked.call_args.kwargs["form"]["hashes"], "abc")

    def test_unauthorized_request_relogs_once(self):
        client = QBitClient("http://qbit:8080", "admin", "secret")
        client.cookie = "SID=expired"
        responses = [
            Response(403, b"Forbidden", {}),
            Response(204, b"", {"Set-Cookie": "SID=new; path=/"}),
            Response(200, b"ok", {}),
        ]
        with patch("islandbot.clients.fetch", side_effect=responses) as mocked:
            result = client.request("/api/v2/torrents/info")
        self.assertEqual(result.text, "ok")
        self.assertEqual(mocked.call_count, 3)


if __name__ == "__main__":
    unittest.main()
