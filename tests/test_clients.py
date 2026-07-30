import unittest
from unittest.mock import patch

from islandbot.clients import EmbyClient, QBitClient
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


class EmbyClientTests(unittest.TestCase):
    def test_create_viewer_sets_password_and_restricted_policy(self):
        responses = [
            Response(200, b"[]", {}),
            Response(
                200,
                b'{"Id":"user-1","Name":"friend","Policy":{"EnableAllFolders":false}}',
                {},
            ),
            Response(204, b"", {}),
            Response(204, b"", {}),
        ]
        client = EmbyClient("http://emby:8096", "api-key")
        with patch("islandbot.clients.fetch", side_effect=responses) as mocked:
            result = client.create_viewer("friend", "123456")

        self.assertEqual(result["username"], "friend")
        password_call = mocked.call_args_list[2]
        self.assertEqual(password_call.kwargs["json_body"]["NewPw"], "123456")
        policy = mocked.call_args_list[3].kwargs["json_body"]
        self.assertFalse(policy["IsAdministrator"])
        self.assertFalse(policy["EnableContentDeletion"])
        self.assertFalse(policy["EnableContentDownloading"])
        self.assertTrue(policy["EnableMediaPlayback"])
        self.assertTrue(policy["EnableAllFolders"])

    def test_existing_username_is_rejected_case_insensitively(self):
        client = EmbyClient("http://emby:8096", "api-key")
        with patch(
            "islandbot.clients.fetch",
            return_value=Response(200, b'[{"Name":"Friend"}]', {}),
        ) as mocked:
            with self.assertRaisesRegex(RuntimeError, "已存在"):
                client.create_viewer("friend", "123456")
        self.assertEqual(mocked.call_count, 1)

    def test_failed_policy_removes_half_created_user(self):
        responses = [
            Response(200, b"[]", {}),
            Response(200, b'{"Id":"user-2","Name":"friend"}', {}),
            Response(204, b"", {}),
            Response(500, b"policy failed", {}),
            Response(204, b"", {}),
        ]
        client = EmbyClient("http://emby:8096", "api-key")
        with patch("islandbot.clients.fetch", side_effect=responses) as mocked:
            with self.assertRaisesRegex(RuntimeError, "设置 Emby 观看权限失败"):
                client.create_viewer("friend", "123456")
        self.assertEqual(mocked.call_args_list[-1].kwargs["method"], "DELETE")


if __name__ == "__main__":
    unittest.main()
