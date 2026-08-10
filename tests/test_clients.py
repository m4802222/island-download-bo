import unittest
from unittest.mock import patch

from islandbot.clients import EmbyClient, QBitClient
from islandbot.http import Response


class QBitClientTests(unittest.TestCase):
    def test_category_sync_creates_updates_and_removes_unused_legacy(self):
        client = QBitClient("http://qbit:8080", "admin", "secret")
        responses = [
            Response(
                200,
                '{"华语电影":{"savePath":"/old"},"欧美电影":{"savePath":"/legacy"},"学校救号":{"savePath":""}}'.encode(),
                {},
            ),
            Response(200, b"", {}),
            Response(200, b"", {}),
            Response(200, b"[]", {}),
            Response(200, b"", {}),
            Response(
                200,
                '{"华语电影":{"savePath":"/downloads/complete/华语电影"},"海外电影":{"savePath":"/downloads/complete/海外电影"},"学校救号":{"savePath":""}}'.encode(),
                {},
            ),
        ]
        with patch.object(client, "request", side_effect=responses) as mocked:
            result = client.sync_categories(
                {
                    "华语电影": "/downloads/complete/华语电影",
                    "海外电影": "/downloads/complete/海外电影",
                },
                {"欧美电影", "欧美动漫", "欧美剧集"},
            )
        paths = [call.args[0] for call in mocked.call_args_list]
        self.assertIn("/api/v2/torrents/editCategory", paths)
        self.assertIn("/api/v2/torrents/createCategory", paths)
        self.assertIn("/api/v2/torrents/removeCategories", paths)
        self.assertEqual(result["updated"], ["华语电影"])
        self.assertEqual(result["created"], ["海外电影"])
        self.assertEqual(result["removed"], ["欧美电影"])

    def test_category_sync_keeps_legacy_category_used_by_a_task(self):
        client = QBitClient("http://qbit:8080", "admin", "secret")
        responses = [
            Response(200, '{"欧美电影":{"savePath":"/legacy"}}'.encode(), {}),
            Response(200, '[{"category":"欧美电影"}]'.encode(), {}),
            Response(200, '{"欧美电影":{"savePath":"/legacy"}}'.encode(), {}),
        ]
        with patch.object(client, "request", side_effect=responses) as mocked:
            result = client.sync_categories({}, {"欧美电影"})
        self.assertEqual(mocked.call_count, 3)
        self.assertEqual(result["kept"], ["欧美电影"])
        self.assertEqual(result["removed"], [])

    def test_category_sync_rejects_unapplied_change(self):
        client = QBitClient("http://qbit:8080", "admin", "secret")
        responses = [
            Response(200, b"{}", {}),
            Response(200, b"", {}),
            Response(200, b"[]", {}),
            Response(200, b"{}", {}),
        ]
        with patch.object(client, "request", side_effect=responses):
            with self.assertRaisesRegex(RuntimeError, "分类同步未生效"):
                client.sync_categories(
                    {"海外电影": "/downloads/complete/海外电影"},
                    set(),
                )

    def test_torrent_file_uses_moviepilot_inbox(self):
        client = QBitClient("http://qbit:8080", "admin", "secret")
        client.cookie = "SID=test"
        with patch(
            "islandbot.clients.fetch",
            return_value=Response(200, b"", {}),
        ) as mocked:
            client.add_torrent(
                "show.torrent",
                b"d4:infode",
                "__auto__",
                "/downloads/complete/islandbot",
            )
        body = mocked.call_args.kwargs["body"]
        self.assertIn(b'name="savepath"', body)
        self.assertIn(b"/downloads/complete/islandbot", body)

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

    def test_rename_file_uses_qbittorrent_endpoint(self):
        client = QBitClient("http://qbit:8080", "admin", "secret")
        client.cookie = "SID=test"
        with patch(
            "islandbot.clients.fetch",
            return_value=Response(200, b"", {}),
        ) as mocked:
            client.rename_file("abc", "08.mkv", "Show.S01E08.mkv")
        self.assertIn("/api/v2/torrents/renameFile", mocked.call_args.args[0])
        self.assertEqual(
            mocked.call_args.kwargs["form"],
            {"hash": "abc", "oldPath": "08.mkv", "newPath": "Show.S01E08.mkv"},
        )

    def test_set_location_uses_qbittorrent_endpoint(self):
        client = QBitClient("http://qbit:8080", "admin", "secret")
        client.cookie = "SID=test"
        with patch(
            "islandbot.clients.fetch",
            return_value=Response(200, b"", {}),
        ) as mocked:
            client.set_location("abc", "/downloads/complete/islandbot")
        self.assertIn("/api/v2/torrents/setLocation", mocked.call_args.args[0])
        self.assertEqual(
            mocked.call_args.kwargs["form"],
            {"hashes": "abc", "location": "/downloads/complete/islandbot"},
        )

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
