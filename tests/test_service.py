from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from ytb_tg_backup.config import load_config
from ytb_tg_backup.service import BackupService


class BackupServiceTest(unittest.TestCase):
    def test_initial_feed_seed_keeps_only_latest_entry_seen(self):
        xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>yt:video:latest1</id>
    <yt:videoId>latest1</yt:videoId>
    <title>Latest</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=latest1"/>
    <published>2026-06-07T03:00:00+00:00</published>
  </entry>
  <entry>
    <id>yt:video:older22</id>
    <yt:videoId>older22</yt:videoId>
    <title>Older</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=older22"/>
    <published>2026-06-07T02:00:00+00:00</published>
  </entry>
  <entry>
    <id>yt:video:oldest3</id>
    <yt:videoId>oldest3</yt:videoId>
    <title>Oldest</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=oldest3"/>
    <published>2026-06-07T01:00:00+00:00</published>
  </entry>
</feed>
"""
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                f"""
[app]
data_dir = "{tmp}"

[[channels]]
id = "asmr"
name = "ASMR"
channel_id = "UC123"
routes = ["live"]
enabled = true
""".strip()
            )
            config = load_config(config_path)
            service = BackupService(config)
            with mock.patch("ytb_tg_backup.service.fetch_feed", return_value=xml):
                service.poll_once(process=False)

            conn = sqlite3.connect(config.db_path)
            rows = conn.execute("SELECT video_id, status FROM videos ORDER BY published_at DESC").fetchall()
            conn.close()
            service.store.close()

        self.assertEqual(rows, [("latest1", "seen"), ("older22", "ignored"), ("oldest3", "ignored")])


if __name__ == "__main__":
    unittest.main()
