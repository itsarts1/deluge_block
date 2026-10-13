import logging
import tempfile
import unittest
from pathlib import Path

import block


def torrent(name, files, priorities):
    return {
        b"name": name.encode("utf-8"),
        b"files": [{b"path": file_name.encode("utf-8")} for file_name in files],
        b"file_priorities": priorities,
    }


class FakeClient:
    def __init__(self, status_responses=None, failures=None):
        self.status_responses = list(status_responses or [])
        self.failures = set(failures or [])
        self.calls = []

    def call(self, method, *args):
        self.calls.append((method, args))
        if method in self.failures:
            raise RuntimeError(f"{method} failed")
        if method == "core.get_torrents_status":
            if self.status_responses:
                return self.status_responses.pop(0)
            return {}
        return True

    def calls_for(self, method):
        return [call for call in self.calls if call[0] == method]


class TorrentBlockerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logging.disable(logging.CRITICAL)

    @classmethod
    def tearDownClass(cls):
        logging.disable(logging.NOTSET)

    def make_blocker(self, client, forbidden=None, unwanted=None, delete_data=False):
        return block.TorrentBlocker(
            client,
            block.normalize_extensions(forbidden or ".exe"),
            block.normalize_extensions(unwanted or ".jpg,.nfo"),
            delete_data,
        )

    def test_forbidden_extension_matching_is_case_insensitive_and_stripped(self):
        client = FakeClient(
            [
                {
                    b"id-1": torrent("bad", ["folder/Setup.EXE"], [1]),
                }
            ]
        )
        blocker = self.make_blocker(client, forbidden=" .Exe ")

        blocker.scan_once()

        self.assertEqual(
            client.calls_for("core.remove_torrent"),
            [("core.remove_torrent", ("id-1", False))],
        )

    def test_duplicate_torrent_names_are_cached_by_id(self):
        client = FakeClient(
            [
                {
                    b"id-1": torrent("same name", ["movie.mkv"], [5]),
                    b"id-2": torrent("same name", ["movie.mkv"], [5]),
                },
                {
                    b"id-1": torrent("same name", ["movie.mkv"], [5]),
                    b"id-2": torrent("same name", ["movie.mkv"], [5]),
                },
            ]
        )
        blocker = self.make_blocker(client)

        blocker.scan_once()
        blocker.scan_once()

        self.assertEqual(len(client.calls_for("core.set_torrent_options")), 2)
        self.assertEqual(blocker.checked_torrent_ids, {"id-1", "id-2"})

    def test_unavailable_file_metadata_is_retried_later(self):
        client = FakeClient(
            [
                {b"id-1": {b"name": b"pending", b"files": [], b"file_priorities": []}},
                {b"id-1": torrent("pending", ["ready.mkv"], [5])},
            ]
        )
        blocker = self.make_blocker(client)

        blocker.scan_once()
        self.assertNotIn("id-1", blocker.checked_torrent_ids)
        blocker.scan_once()

        self.assertEqual(len(client.calls_for("core.set_torrent_options")), 1)
        self.assertIn("id-1", blocker.checked_torrent_ids)

    def test_mismatched_file_and_priority_counts_are_not_processed_or_cached(self):
        client = FakeClient(
            [
                {
                    b"id-1": torrent("mismatch", ["movie.mkv", "cover.jpg"], [5]),
                }
            ]
        )
        blocker = self.make_blocker(client)

        blocker.scan_once()

        self.assertEqual(client.calls_for("core.set_torrent_options"), [])
        self.assertEqual(client.calls_for("core.remove_torrent"), [])
        self.assertNotIn("id-1", blocker.checked_torrent_ids)

    def test_forbidden_files_are_removed_even_when_priority_is_zero(self):
        client = FakeClient(
            [
                {
                    b"id-1": torrent("already skipped", ["bad.exe"], [0]),
                }
            ]
        )
        blocker = self.make_blocker(client)

        blocker.scan_once()

        self.assertEqual(
            client.calls_for("core.remove_torrent"),
            [("core.remove_torrent", ("id-1", False))],
        )
        self.assertEqual(client.calls_for("core.set_torrent_options"), [])

    def test_unwanted_files_are_skipped_while_other_priorities_are_preserved(self):
        client = FakeClient(
            [
                {
                    b"id-1": torrent(
                        "extras",
                        ["movie.mkv", "cover.JPG", "readme.nfo"],
                        [5, 4, 0],
                    ),
                }
            ]
        )
        blocker = self.make_blocker(client)

        blocker.scan_once()

        self.assertEqual(
            client.calls_for("core.set_torrent_options"),
            [
                (
                    "core.set_torrent_options",
                    (["id-1"], {"file_priorities": [5, 0, 0]}),
                )
            ],
        )

    def test_get_status_rpc_errors_do_not_escape_scan(self):
        client = FakeClient([{}], failures={"core.get_torrents_status"})
        blocker = self.make_blocker(client)

        self.assertFalse(blocker.scan_once())

    def test_action_rpc_errors_do_not_cache_torrent(self):
        client = FakeClient(
            [
                {b"id-1": torrent("extras", ["cover.jpg"], [4])},
                {b"id-1": torrent("extras", ["cover.jpg"], [4])},
            ],
            failures={"core.set_torrent_options"},
        )
        blocker = self.make_blocker(client)

        blocker.scan_once()
        client.failures.clear()
        blocker.scan_once()

        self.assertEqual(len(client.calls_for("core.set_torrent_options")), 2)
        self.assertIn("id-1", blocker.checked_torrent_ids)


class ConfigTests(unittest.TestCase):
    def test_load_config_uses_safe_defaults(self):
        contents = """
[login]
host=localhost
port=58846
user=localclient
password=secret

[lists]
forbidden=.exe, .EXE
unwanted=.jpg
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.ini"
            config_path.write_text(contents, encoding="utf-8")

            settings = block.load_config(config_path)

        self.assertFalse(settings.delete_downloaded_data)
        self.assertEqual(settings.scan_interval_seconds, 10)
        self.assertEqual(settings.forbidden_extensions, [".exe", ".exe"])

    def test_load_config_accepts_scan_interval_override(self):
        contents = """
[login]
host=localhost
port=58846
user=localclient
password=secret

[lists]
forbidden=.exe
unwanted=.jpg

[behavior]
scan_interval_seconds=5
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.ini"
            config_path.write_text(contents, encoding="utf-8")

            settings = block.load_config(config_path)

        self.assertEqual(settings.scan_interval_seconds, 5)

    def test_missing_required_config_reports_key_without_password_value(self):
        contents = """
[login]
host=localhost
port=58846
user=localclient

[lists]
forbidden=.exe
unwanted=.jpg
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.ini"
            config_path.write_text(contents, encoding="utf-8")

            with self.assertRaises(block.ConfigError) as error:
                block.load_config(config_path)

        self.assertIn("[login] password", str(error.exception))
        self.assertNotIn("secret", str(error.exception))


if __name__ == "__main__":
    unittest.main()
