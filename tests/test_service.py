from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from ytb_tg_backup.config import load_config
from ytb_tg_backup.extension_api import HttpResponse
from ytb_tg_backup.feed import FeedEntry
from ytb_tg_backup.models import Origin
from ytb_tg_backup.service import BackupService
from ytb_tg_backup.source_filter import SOURCE_FILTER_STATE_KEY


class BackupServiceTest(unittest.TestCase):
    def test_cleanup_delivered_artifacts_applies_process_and_master_policies(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                f'''[app]
data_dir = "{tmp}"

[storage]
process_retention_hours = 24
backup_retention_hours = 72
''',
                encoding="utf-8",
            )
            service = BackupService(load_config(config_path))
            store = mock.Mock()
            store.list_process_retention_candidates.return_value = [
                {
                    "group_key": "media:1",
                    "artifact_id": 1,
                    "artifact_state": "ready",
                },
                {
                    "group_key": "media:2",
                    "artifact_id": 2,
                    "artifact_state": "ready",
                },
            ]
            store.list_delivery_retention_candidates.return_value = [
                {
                    "group_key": "stream-a",
                    "artifact_id": 10,
                    "artifact_state": "suppressed",
                },
                {
                    "group_key": "stream-a",
                    "artifact_id": 11,
                    "artifact_state": "ready",
                },
                {
                    "group_key": "stream-b",
                    "artifact_id": 20,
                    "artifact_state": "ready",
                },
            ]
            store.get_disk_resource.side_effect = lambda artifact_id, _root: {
                "resource_revision": f"revision-{artifact_id}"
            }

            def purge_process(artifact_id, _root, **_kwargs):
                if artifact_id == 2:
                    raise OSError("process cleanup failed")
                return {
                    "completed": True,
                    "deleted_files": 2,
                    "missing_files": 0,
                    "freed_bytes": 100,
                    "errors": [],
                }

            def purge_master(artifact_id, _root, **_kwargs):
                if artifact_id == 10:
                    return {
                        "completed": False,
                        "skipped": True,
                        "deleted_files": 0,
                        "missing_files": 0,
                        "freed_bytes": 0,
                        "errors": [],
                    }
                return {
                    "completed": True,
                    "deleted_files": 1,
                    "missing_files": 1,
                    "freed_bytes": 200,
                    "errors": [],
                }

            store.purge_process_artifacts.side_effect = purge_process
            store.purge_disk_resource.side_effect = purge_master
            fixed_now = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)

            try:
                with mock.patch("ytb_tg_backup.service.datetime") as clock:
                    clock.now.return_value = fixed_now
                    summary = service._cleanup_delivered_artifacts_once(store)
            finally:
                service.close()

        process_cutoff = "2026-08-10T12:00:00+00:00"
        master_cutoff = "2026-08-08T12:00:00+00:00"
        store.list_process_retention_candidates.assert_called_once_with(
            process_cutoff,
            limit=100,
        )
        store.list_delivery_retention_candidates.assert_called_once_with(
            master_cutoff,
            limit=100,
        )
        self.assertEqual(
            [call.args[0] for call in store.get_disk_resource.call_args_list],
            [1, 2, 10, 20],
        )
        self.assertEqual(
            [call.args[0] for call in store.purge_process_artifacts.call_args_list],
            [1, 2],
        )
        self.assertEqual(
            [call.args[0] for call in store.purge_disk_resource.call_args_list],
            [10, 20],
        )
        for call in store.purge_process_artifacts.call_args_list:
            self.assertEqual(call.kwargs["delivered_before"], process_cutoff)
            self.assertNotIn("source", call.kwargs)
            self.assertNotIn("delivery_retention_before", call.kwargs)
        for call in store.purge_disk_resource.call_args_list:
            self.assertEqual(call.kwargs["source"], "delivery_retention")
            self.assertEqual(
                call.kwargs["delivery_retention_before"],
                master_cutoff,
            )
        self.assertEqual(
            summary,
            {
                "candidates": 5,
                "resources": 2,
                "process_candidates": 2,
                "process_resources": 1,
                "master_candidates": 3,
                "master_resources": 1,
                "archive_candidates": 0,
                "archive_resources": 0,
                "archive_cleanup_sources": 0,
                "archive_source_deleted": 0,
                "archive_copied_bytes": 0,
                "deleted_files": 3,
                "missing_files": 1,
                "skipped_resources": 1,
                "failed_resources": 1,
                "freed_bytes": 300,
            },
        )

    def test_cleanup_delivered_artifacts_is_disabled_at_zero_hours(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                f'[app]\ndata_dir = "{tmp}"',
                encoding="utf-8",
            )
            service = BackupService(load_config(config_path))
            store = mock.Mock()
            try:
                summary = service._cleanup_delivered_artifacts_once(store)
            finally:
                service.close()

        store.list_process_retention_candidates.assert_not_called()
        store.list_delivery_retention_candidates.assert_not_called()
        store.list_master_archive_candidates.assert_not_called()
        store.list_archive_source_cleanup_candidates.assert_not_called()
        store.validate_archive_destination.assert_not_called()
        self.assertEqual(summary["candidates"], 0)
        self.assertEqual(summary["resources"], 0)
        self.assertEqual(summary["skipped_resources"], 0)
        self.assertEqual(summary["freed_bytes"], 0)

    def test_artifact_cleanup_loop_sweeps_immediately_then_waits_one_hour(self):
        class StopAfterFirstWait:
            def __init__(self, events):
                self.stopped = False
                self.events = []
                self.ordered_events = events

            def is_set(self):
                return self.stopped

            def wait(self, seconds):
                self.events.append(("wait", seconds))
                self.ordered_events.append(("wait", seconds))
                self.stopped = True
                return True

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                f'''[app]
data_dir = "{tmp}"

[storage]
process_retention_hours = 24
backup_retention_hours = 0
''',
                encoding="utf-8",
            )
            service = BackupService(load_config(config_path))
            events = []
            stop_event = StopAfterFirstWait(events)
            service._stop_event = stop_event
            worker_store = mock.Mock()

            def sweep(store):
                self.assertIs(store, worker_store)
                events.append("sweep")

            try:
                with (
                    mock.patch(
                        "ytb_tg_backup.service.Store",
                        return_value=worker_store,
                    ) as store_factory,
                    mock.patch.object(
                        service,
                        "_cleanup_delivered_artifacts_once",
                        side_effect=sweep,
                    ),
                ):
                    service._artifact_cleanup_loop()
            finally:
                service.close()

        store_factory.assert_called_once_with(service.config.db_path)
        worker_store.initialize.assert_called_once_with()
        worker_store.close.assert_called_once_with()
        self.assertEqual(events, ["sweep", ("wait", 3600)])
        self.assertEqual(stop_event.events, [("wait", 3600)])

    def test_run_forever_only_starts_artifact_cleanup_worker_when_enabled(self):
        for process_hours, master_hours, archive_enabled, expected in (
            (0, 0, False, False),
            (24, 0, False, True),
            (0, 720, False, True),
            (0, 0, True, True),
        ):
            with self.subTest(
                process_hours=process_hours,
                master_hours=master_hours,
                archive_enabled=archive_enabled,
            ):
                with tempfile.TemporaryDirectory() as tmp:
                    config_path = Path(tmp) / "config.toml"
                    archive_line = (
                        f'archive_dir = "{Path(tmp) / "archive"}"\n'
                        if archive_enabled
                        else ""
                    )
                    config_path.write_text(
                        f'''[app]
data_dir = "{tmp}"

[storage]
process_retention_hours = {process_hours}
backup_retention_hours = {master_hours}
{archive_line}
''',
                        encoding="utf-8",
                    )
                    service = BackupService(load_config(config_path))
                    threads = []

                    def thread_factory(*_args, **kwargs):
                        worker = mock.Mock()
                        worker.name = kwargs["name"]
                        worker.is_alive.return_value = False
                        threads.append((worker, kwargs))
                        return worker

                    try:
                        with (
                            mock.patch.object(service, "initialize"),
                            mock.patch.object(service, "_log_startup_warnings"),
                            mock.patch.object(
                                service._stop_event,
                                "wait",
                                return_value=True,
                            ),
                            mock.patch(
                                "ytb_tg_backup.service.threading.Thread",
                                side_effect=thread_factory,
                            ),
                        ):
                            service.run_forever()
                    finally:
                        if not service.store._closed:
                            service.close()

                cleanup_threads = [
                    kwargs
                    for _worker, kwargs in threads
                    if kwargs["name"] == "artifact-cleanup-worker"
                ]
                self.assertEqual(bool(cleanup_threads), expected)
                if cleanup_threads:
                    self.assertEqual(
                        cleanup_threads[0]["target"],
                        service._artifact_cleanup_loop,
                    )
                    self.assertTrue(cleanup_threads[0]["daemon"])

    def test_cleanup_archives_masters_before_retention_policies(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive_dir = Path(tmp) / "archive"
            archive_dir.mkdir()
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                f'''[app]
data_dir = "{tmp}"

[storage]
archive_dir = "{archive_dir}"
archive_after_delivery_hours = 12
archive_require_mount = false
''',
                encoding="utf-8",
            )
            service = BackupService(load_config(config_path))
            store = mock.Mock()
            store.list_archive_source_cleanup_candidates.return_value = [90]
            store.cleanup_archived_master_source.return_value = {
                "completed": True,
                "source_deleted": True,
                "errors": [],
            }
            store.list_master_archive_candidates.return_value = [
                {"group_key": "media:1", "artifact_id": 1},
                {"group_key": "media:2", "artifact_id": 2},
            ]
            store.get_disk_resource.side_effect = lambda artifact_id, _roots: {
                "resource_revision": f"revision-{artifact_id}"
            }

            def archive_master(artifact_id, *_args, **_kwargs):
                if artifact_id == 2:
                    raise OSError("mounted archive unavailable")
                return {
                    "completed": True,
                    "archived": True,
                    "skipped": False,
                    "copied_bytes": 100,
                    "source_deleted": True,
                    "errors": [],
                }

            store.archive_master.side_effect = archive_master
            fixed_now = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            try:
                with mock.patch("ytb_tg_backup.service.datetime") as clock:
                    clock.now.return_value = fixed_now
                    summary = service._cleanup_delivered_artifacts_once(store)
            finally:
                service.close()

        cutoff = "2026-08-11T00:00:00+00:00"
        store.list_archive_source_cleanup_candidates.assert_called_once_with(
            limit=100,
        )
        store.cleanup_archived_master_source.assert_called_once_with(
            90,
            service.config.managed_storage_roots,
        )
        store.validate_archive_destination.assert_called_once_with(
            service.config.download_dir,
            archive_dir,
            require_mount=False,
        )
        store.list_master_archive_candidates.assert_called_once_with(
            cutoff,
            limit=100,
        )
        self.assertEqual(
            [call.args[0] for call in store.archive_master.call_args_list],
            [1, 2],
        )
        first_call = store.archive_master.call_args_list[0]
        self.assertEqual(first_call.args[1], service.config.download_dir)
        self.assertEqual(first_call.args[2], archive_dir)
        self.assertEqual(first_call.kwargs["delivered_before"], cutoff)
        self.assertFalse(first_call.kwargs["require_mount"])
        store.list_process_retention_candidates.assert_not_called()
        store.list_delivery_retention_candidates.assert_not_called()
        self.assertEqual(summary["candidates"], 2)
        self.assertEqual(summary["resources"], 1)
        self.assertEqual(summary["archive_candidates"], 2)
        self.assertEqual(summary["archive_resources"], 1)
        self.assertEqual(summary["archive_cleanup_sources"], 1)
        self.assertEqual(summary["archive_source_deleted"], 2)
        self.assertEqual(summary["archive_copied_bytes"], 100)
        self.assertEqual(summary["failed_resources"], 1)

    def test_cleanup_skips_new_archive_transfers_when_mount_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive_dir = Path(tmp) / "archive"
            archive_dir.mkdir()
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                f'''[app]
data_dir = "{tmp}"

[storage]
archive_dir = "{archive_dir}"
''',
                encoding="utf-8",
            )
            service = BackupService(load_config(config_path))
            store = mock.Mock()
            store.list_archive_source_cleanup_candidates.return_value = []
            store.validate_archive_destination.side_effect = ValueError(
                "archive directory is not on a non-root mount"
            )
            try:
                summary = service._cleanup_delivered_artifacts_once(store)
            finally:
                service.close()

        store.list_master_archive_candidates.assert_not_called()
        store.get_disk_resource.assert_not_called()
        store.archive_master.assert_not_called()
        self.assertEqual(summary["archive_candidates"], 0)
        self.assertEqual(summary["archive_resources"], 0)
        self.assertEqual(summary["failed_resources"], 0)

    def test_control_worker_bot_inherits_runtime_dependencies(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                f'''[app]
data_dir = "{tmp}"

[telegram]
bot_token = "test-token"

[control]
enabled = true
''',
                encoding="utf-8",
            )
            service = BackupService(load_config(config_path))
            worker_bot = mock.Mock()
            worker_bot.process_once.side_effect = service.stop

            try:
                with mock.patch(
                    "ytb_tg_backup.service.ControlBot",
                    return_value=worker_bot,
                ) as bot_factory:
                    service._control_loop()

                bot_factory.assert_called_once()
                args, kwargs = bot_factory.call_args
                self.assertIs(args[0], service.config)
                self.assertEqual(args[1].path, service.store.path)
                self.assertIs(args[2], service.logger)
                self.assertIs(kwargs["connection"], service.runtime.connection)
                self.assertIs(kwargs["providers"], service.runtime.providers)
                worker_bot.process_once.assert_called_once_with()
            finally:
                service.close()

    def test_stale_poll_detection_covers_disabled_deleted_and_retargeted_origins(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.toml"
            config_path.write_text(
                f'[app]\ndata_dir = "{root}"',
                encoding="utf-8",
            )
            service = BackupService(load_config(config_path))
            service.store.initialize()
            polled = Origin("yt", "youtube", "uploads", "YT", "UCabc")
            service.store.upsert_origin(polled, managed_by="catalog")

            self.assertTrue(
                service._origin_poll_configuration_is_current(service.store, polled)
            )

            service.store.upsert_origin(
                Origin("yt", "youtube", "uploads", "YT", "UCabc", enabled=False),
                managed_by="catalog",
            )
            self.assertFalse(
                service._origin_poll_configuration_is_current(service.store, polled)
            )

            service.store.conn.execute("DELETE FROM origins WHERE id='yt'")
            service.store.conn.commit()
            self.assertFalse(
                service._origin_poll_configuration_is_current(service.store, polled)
            )

            service.store.upsert_origin(
                Origin("yt", "youtube", "uploads", "YT", "UCABC"),
                managed_by="catalog",
            )
            self.assertFalse(
                service._origin_poll_configuration_is_current(service.store, polled)
            )
            service.store.close()

    def test_initialize_hardens_all_provider_archive_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.toml"
            config_path.write_text(f'[app]\ndata_dir = "{tmp_path}"')
            config = load_config(config_path)
            service = BackupService(config)
            youtube_archive = service.downloader.archive_file_for_provider("youtube")
            twitch_archive = service.downloader.archive_file_for_provider("twitch")
            for path in (youtube_archive, twitch_archive):
                path.write_text("seed")
                path.chmod(0o666)

            service.initialize()

            self.assertEqual(youtube_archive.stat().st_mode & 0o777, 0o600)
            self.assertEqual(twitch_archive.stat().st_mode & 0o777, 0o600)
            service.store.close()

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
            with mock.patch(
                "ytb_tg_backup.network.UrllibHttpTransport.request",
                return_value=HttpResponse(200, "https://example.test/feed", {}, xml),
            ):
                service.poll_once(process=False)
                service.poll_once(process=False)

            conn = sqlite3.connect(config.db_path)
            rows = conn.execute("SELECT video_id, status FROM videos ORDER BY published_at DESC").fetchall()
            job_count = conn.execute("SELECT COUNT(*) FROM jobs WHERE job_type='download'").fetchone()[0]
            conn.close()
            service.store.close()

        self.assertEqual(rows, [("latest1", "seen"), ("older22", "ignored"), ("oldest3", "ignored")])
        self.assertEqual(job_count, 1)

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

        def fake_fetch(request, _route):
            if "UC123" in request.url:
                body = source_match_xml
            elif "UC456" in request.url:
                body = title_match_xml
            else:
                body = filtered_xml
            return HttpResponse(200, request.url, {}, body)

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
            with mock.patch(
                "ytb_tg_backup.network.UrllibHttpTransport.request",
                side_effect=fake_fetch,
            ) as fetch:
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
            with mock.patch(
                "ytb_tg_backup.network.UrllibHttpTransport.request",
                return_value=HttpResponse(200, "https://example.test/feed", {}, xml),
            ) as fetch:
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
            with mock.patch(
                "ytb_tg_backup.network.UrllibHttpTransport.request",
                return_value=HttpResponse(200, "https://example.test/feed", {}, xml),
            ):
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
