from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from ytb_tg_backup.config import load_config
from ytb_tg_backup.feed import FeedEntry
from ytb_tg_backup.service import BackupService
from ytb_tg_backup.source_filter import SOURCE_FILTER_STATE_KEY


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

    def test_source_filter_defaults_to_case_insensitive_asmr(self):
        source_match_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>yt:video:source1</id>
    <yt:videoId>source1</yt:videoId>
    <title>Latest</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=source1"/>
    <published>2026-06-07T03:00:00+00:00</published>
  </entry>
</feed>
"""
        title_match_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>yt:video:title1</id>
    <yt:videoId>title1</yt:videoId>
    <title>ASMR sleep stream</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=title1"/>
    <published>2026-06-07T03:00:00+00:00</published>
  </entry>
</feed>
"""
        filtered_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>yt:video:game1</id>
    <yt:videoId>game1</yt:videoId>
    <title>Game stream</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=game1"/>
    <published>2026-06-07T03:00:00+00:00</published>
  </entry>
</feed>
"""

        def fake_fetch(url: str) -> bytes:
            if "UC123" in url:
                return source_match_xml
            if "UC456" in url:
                return title_match_xml
            return filtered_xml

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                f"""
[app]
data_dir = "{tmp}"

[[channels]]
id = "asmr"
name = "soft asmr"
channel_id = "UC123"
routes = ["live"]
enabled = true

[[channels]]
id = "news"
name = "News"
channel_id = "UC456"
routes = ["live"]
enabled = true

[[channels]]
id = "game"
name = "Game"
channel_id = "UC789"
routes = ["live"]
enabled = true
""".strip()
            )
            config = load_config(config_path)
            service = BackupService(config)
            with mock.patch("ytb_tg_backup.service.fetch_feed", side_effect=fake_fetch) as fetch:
                service.poll_once(process=False)
            conn = sqlite3.connect(config.db_path)
            rows = conn.execute(
                "SELECT video_id, status, last_error FROM videos ORDER BY video_id ASC"
            ).fetchall()
            conn.close()
            service.store.close()

        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(
            rows,
            [
                ("game1", "ignored", "source filter ignored: /ASMR/i"),
                ("source1", "seen", None),
                ("title1", "seen", None),
            ],
        )

    def test_source_filter_can_be_disabled(self):
        xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>yt:video:latest1</id>
    <yt:videoId>latest1</yt:videoId>
    <title>Latest</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=latest1"/>
    <published>2026-06-07T03:00:00+00:00</published>
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

[[channels]]
id = "news"
name = "News"
channel_id = "UC456"
routes = ["live"]
enabled = true
""".strip()
            )
            config = load_config(config_path)
            service = BackupService(config)
            service.initialize()
            service.store.set_bot_state(SOURCE_FILTER_STATE_KEY, "")
            with mock.patch("ytb_tg_backup.service.fetch_feed", return_value=xml) as fetch:
                service.poll_once(process=False)
            service.store.close()

        self.assertEqual(fetch.call_count, 2)

    def test_source_filter_initial_seed_keeps_latest_matching_entry(self):
        xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>yt:video:game1</id>
    <yt:videoId>game1</yt:videoId>
    <title>Game stream</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=game1"/>
    <published>2026-06-07T03:00:00+00:00</published>
  </entry>
  <entry>
    <id>yt:video:asmr1</id>
    <yt:videoId>asmr1</yt:videoId>
    <title>ASMR sleep stream</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=asmr1"/>
    <published>2026-06-07T02:00:00+00:00</published>
  </entry>
  <entry>
    <id>yt:video:asmr2</id>
    <yt:videoId>asmr2</yt:videoId>
    <title>ASMR ear cleaning</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=asmr2"/>
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
id = "mixed"
name = "Mixed"
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
            rows = conn.execute(
                "SELECT video_id, status, last_error FROM videos ORDER BY published_at DESC"
            ).fetchall()
            conn.close()
            service.store.close()

        self.assertEqual(
            rows,
            [
                ("game1", "ignored", "source filter ignored: /ASMR/i"),
                ("asmr1", "seen", None),
                ("asmr2", "ignored", "initial feed seed ignored; kept latest entry only"),
            ],
        )

    def test_process_pending_ignores_existing_nonmatching_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                f"""
[app]
data_dir = "{tmp}"
""".strip()
            )
            config = load_config(config_path)
            service = BackupService(config)
            service.initialize()
            service.store.upsert_entry(
                FeedEntry(
                    feed_id="db:live@Patra_Suou",
                    feed_name="周防パトラ (live)",
                    video_id="game1",
                    title="Game stream",
                    url="https://www.youtube.com/watch?v=game1",
                    published_at=None,
                    updated_at=None,
                )
            )
            with (
                mock.patch.object(service.downloader, "check_tools", return_value=[]),
                mock.patch.object(service.downloader, "probe") as probe,
            ):
                service.process_pending()
            conn = sqlite3.connect(config.db_path)
            row = conn.execute("SELECT status, last_error FROM videos WHERE video_id = 'game1'").fetchone()
            conn.close()
            service.store.close()

        probe.assert_not_called()
        self.assertEqual(row, ("ignored", "source filter ignored: /ASMR/i"))


if __name__ == "__main__":
    unittest.main()
