from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

from ytb_tg_backup.config import load_config
from ytb_tg_backup.dev.youtube_membership import (
    AnonymousYoutubeInspector,
    ProbeOutcome,
    SurfaceItem,
    YoutubeMembershipDevRunner,
    _DevStore,
)
from ytb_tg_backup.store import Store


EMPTY_ATOM = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">
</feed>
"""


def _surface_item(
    video_id: str = "member12345",
    *,
    live_status: str | None = None,
) -> SurfaceItem:
    return SurfaceItem(
        video_id=video_id,
        title="Members-only stream",
        url=f"https://www.youtube.com/watch?v={video_id}",
        availability="subscriber_only",
        live_status=live_status,
    )


class FakeInspector:
    def __init__(self) -> None:
        self.items: list[SurfaceItem] = []
        self.outcome = ProbeOutcome(
            ok=True,
            access_class="members_only",
            availability="subscriber_only",
        )
        self.probe_calls: list[str] = []

    def list_tab(self, channel_id: str, tab: str) -> list[SurfaceItem]:
        return list(self.items)

    def list_members_playlist(self, channel_id: str) -> list[SurfaceItem]:
        return list(self.items)

    def probe(self, url: str) -> ProbeOutcome:
        self.probe_calls.append(url)
        return self.outcome


class FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []

    def send_text(
        self,
        text: str,
        *,
        chat_id: str,
        timeout_seconds: int,
    ) -> int:
        self.messages.append(
            {
                "text": text,
                "chat_id": chat_id,
                "timeout_seconds": timeout_seconds,
            }
        )
        return len(self.messages)


class YoutubeMembershipDevTest(unittest.TestCase):
    def _config(
        self,
        root: Path,
        *,
        notify: bool = True,
        dangerous_download_args: bool = False,
    ):
        extra_args = (
            '["--cookies", "/tmp/secret-cookies.txt", "--exec", "danger"]'
            if dangerous_download_args
            else "[]"
        )
        path = root / "config.toml"
        path.write_text(
            f"""
[app]
data_dir = "{root / 'data'}"

[[origins]]
id = "youtube-members-test"
provider = "youtube"
kind = "uploads"
name = "YouTube Members Test"
external_id = "UC1234567890123456789012"
enabled = false

[download]
yt_dlp = "/tmp/cookie-injecting-wrapper"
extra_args = {extra_args}

[telegram]
bot_token = "test-token"
chat_id = "@default-chat"
upload_timeout_seconds = 45

[dev.youtube_membership]
enabled = true
notify = {str(notify).lower()}
origin_ids = ["youtube-members-test"]
poll_interval_seconds = 300
request_timeout_seconds = 30
request_spacing_seconds = 0
tab_limit = 30
chat_id = "@dev-members-chat"
""".strip(),
            encoding="utf-8",
        )
        return load_config(path)

    def _runner(
        self,
        config,
        inspector: FakeInspector,
        notifier: FakeNotifier,
    ) -> YoutubeMembershipDevRunner:
        return YoutubeMembershipDevRunner(
            config,
            inspector=inspector,
            notifier=notifier,
            feed_fetcher=lambda _url: EMPTY_ATOM,
        )

    def test_anonymous_inspector_ignores_all_download_args_and_user_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(
                Path(tmp),
                dangerous_download_args=True,
            )
            inspector = AnonymousYoutubeInspector(config)
            completed = [
                subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout=json.dumps({"entries": []}),
                    stderr="",
                ),
                subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "id": "member12345",
                            "title": "Members-only stream",
                            "availability": "subscriber_only",
                            "live_status": "is_upcoming",
                            "formats": [],
                        }
                    ),
                    stderr="",
                ),
            ]
            with mock.patch("subprocess.run", side_effect=completed) as run:
                inspector.list_members_playlist("UC1234567890123456789012")
                inspector.probe(
                    "https://www.youtube.com/watch?v=member12345"
                )

        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(len(commands), 2)
        self.assertIn("--flat-playlist", commands[0])
        self.assertIn("--skip-download", commands[1])
        for command in commands:
            self.assertEqual(command[0], "yt-dlp")
            self.assertIn("--ignore-config", command)
            self.assertNotIn("--cookies", command)
            self.assertNotIn("/tmp/secret-cookies.txt", command)
            self.assertNotIn("--exec", command)
            self.assertNotIn("danger", command)

    def test_first_successful_snapshot_is_a_silent_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(Path(tmp))
            inspector = FakeInspector()
            inspector.items = [_surface_item(live_status="is_upcoming")]
            inspector.outcome = ProbeOutcome(
                ok=True,
                access_class="upcoming",
                availability="subscriber_only",
                live_status="is_upcoming",
            )
            notifier = FakeNotifier()
            runner = self._runner(config, inspector, notifier)
            try:
                runner.run_once()
                status = runner.status()
            finally:
                runner.close()

        self.assertEqual(notifier.messages, [])
        self.assertIn("db_path", status)
        self.assertIn("counts", status)
        self.assertIn("cooldown_until", status)
        self.assertIn("recent_notifications", status)

    def test_legacy_surface_rows_are_migrated_as_silent_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.db"
            with sqlite3.connect(path) as connection:
                connection.executescript(
                    """
                    CREATE TABLE surface_state (
                      origin_id TEXT NOT NULL,
                      surface TEXT NOT NULL,
                      seeded_at TEXT,
                      last_success_at TEXT,
                      last_error_at TEXT,
                      last_error_kind TEXT,
                      last_error_message TEXT,
                      PRIMARY KEY (origin_id, surface)
                    );
                    CREATE TABLE surface_items (
                      origin_id TEXT NOT NULL,
                      video_id TEXT NOT NULL,
                      surface TEXT NOT NULL,
                      first_seen_at TEXT NOT NULL,
                      last_seen_at TEXT NOT NULL,
                      PRIMARY KEY (origin_id, video_id, surface)
                    );
                    INSERT INTO surface_state (
                      origin_id, surface, seeded_at, last_success_at
                    ) VALUES (
                      'legacy', 'members_playlist',
                      '2026-01-01T00:00:00+00:00',
                      '2026-01-01T00:00:00+00:00'
                    );
                    INSERT INTO surface_items (
                      origin_id, video_id, surface, first_seen_at, last_seen_at
                    ) VALUES (
                      'legacy', 'old-video', 'members_playlist',
                      '2026-01-01T00:00:00+00:00',
                      '2026-01-01T00:00:00+00:00'
                    );
                    """
                )

            store = _DevStore(path)
            try:
                baseline = store.conn.execute(
                    """
                    SELECT baseline FROM surface_items
                    WHERE origin_id = 'legacy' AND video_id = 'old-video'
                    """
                ).fetchone()[0]
            finally:
                store.close()

        self.assertEqual(baseline, 1)

    def test_baseline_stays_silent_across_a_retryable_probe_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(Path(tmp))
            inspector = FakeInspector()
            inspector.items = [_surface_item(live_status="is_upcoming")]
            inspector.outcome = ProbeOutcome(
                ok=False,
                access_class="probe_error",
                error_kind="no_formats",
                error_message="No video formats found",
            )
            notifier = FakeNotifier()
            runner = self._runner(config, inspector, notifier)
            try:
                runner.run_once()
                inspector.outcome = ProbeOutcome(
                    ok=True,
                    access_class="upcoming",
                    availability="subscriber_only",
                    live_status="is_upcoming",
                )
                runner.run_once()
                self.assertEqual(notifier.messages, [])

                inspector.items = [_surface_item(live_status="is_live")]
                inspector.outcome = ProbeOutcome(
                    ok=True,
                    access_class="members_only",
                    availability="subscriber_only",
                    live_status="is_live",
                )
                runner.run_once()
            finally:
                runner.close()

        self.assertEqual(len(notifier.messages), 1)
        self.assertIn("会员直播开始", notifier.messages[0]["text"])

    def test_baseline_backlog_remains_silent_in_later_cycles(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(Path(tmp))
            inspector = FakeInspector()
            inspector.items = [
                _surface_item(video_id=f"baseline{index:04d}")
                for index in range(10)
            ]
            notifier = FakeNotifier()
            runner = self._runner(config, inspector, notifier)
            try:
                runner.run_once()
                runner.run_once()
            finally:
                runner.close()

        self.assertEqual(len(inspector.probe_calls), 10)
        self.assertEqual(notifier.messages, [])

    def test_new_member_lifecycle_notifies_each_transition_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(Path(tmp))
            inspector = FakeInspector()
            notifier = FakeNotifier()
            runner = self._runner(config, inspector, notifier)
            try:
                runner.run_once()  # Empty baseline.

                inspector.items = [_surface_item(live_status="is_upcoming")]
                inspector.outcome = ProbeOutcome(
                    ok=True,
                    access_class="upcoming",
                    availability="subscriber_only",
                    live_status="is_upcoming",
                )
                runner.run_once()
                runner.run_once()
                self.assertEqual(len(notifier.messages), 1)

                inspector.items = [_surface_item(live_status="is_live")]
                inspector.outcome = ProbeOutcome(
                    ok=True,
                    access_class="members_only",
                    availability="subscriber_only",
                    live_status="is_live",
                )
                runner.run_once()
                runner.run_once()
                self.assertEqual(len(notifier.messages), 2)

                inspector.items = [_surface_item(live_status="post_live")]
                inspector.outcome = ProbeOutcome(
                    ok=True,
                    access_class="members_only",
                    availability="subscriber_only",
                    live_status="post_live",
                )
                runner.run_once()
                runner.run_once()
                self.assertEqual(len(notifier.messages), 3)
            finally:
                runner.close()

        self.assertTrue(
            all(
                message["chat_id"] == "@dev-members-chat"
                for message in notifier.messages
            )
        )

    def test_removed_is_terminal_and_is_not_probed_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(Path(tmp), notify=True)
            inspector = FakeInspector()
            notifier = FakeNotifier()
            runner = self._runner(config, inspector, notifier)
            try:
                runner.run_once()
                inspector.items = [_surface_item(video_id="removed1234")]
                inspector.outcome = ProbeOutcome(
                    ok=False,
                    access_class="removed",
                    error_kind="removed",
                    error_message="Video has been removed by the uploader",
                )
                for _ in range(8):
                    runner.run_once()
            finally:
                runner.close()

        self.assertEqual(len(inspector.probe_calls), 1)
        self.assertEqual(notifier.messages, [])

    def test_no_formats_probe_attempts_are_bounded_at_five(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(Path(tmp), notify=False)
            inspector = FakeInspector()
            notifier = FakeNotifier()
            runner = self._runner(config, inspector, notifier)
            try:
                runner.run_once()
                inspector.items = [_surface_item(video_id="noformats123")]
                inspector.outcome = ProbeOutcome(
                    ok=False,
                    access_class="probe_error",
                    error_kind="no_formats",
                    error_message="No video formats found",
                )
                for _ in range(10):
                    runner.run_once()
            finally:
                runner.close()

        self.assertGreaterEqual(len(inspector.probe_calls), 1)
        self.assertLessEqual(len(inspector.probe_calls), 5)

    def test_completed_member_vod_is_not_reprobed_every_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(Path(tmp), notify=False)
            inspector = FakeInspector()
            notifier = FakeNotifier()
            runner = self._runner(config, inspector, notifier)
            try:
                runner.run_once()
                inspector.items = [_surface_item(video_id="completed123")]
                runner.run_once()
                for _ in range(5):
                    runner.run_once()
            finally:
                runner.close()

        self.assertEqual(len(inspector.probe_calls), 1)

    def test_unqueued_discovery_is_reconciled_after_cycle_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(Path(tmp), notify=False)
            inspector = FakeInspector()
            notifier = FakeNotifier()
            runner = self._runner(config, inspector, notifier)
            try:
                runner.run_once()
                inspector.items = [_surface_item(video_id="crashgap123")]
                with mock.patch.object(
                    runner.store,
                    "queue_probe",
                    side_effect=RuntimeError("simulated crash before queue commit"),
                ):
                    with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                        runner.run_once()
                runner.run_once()
            finally:
                runner.close()

        self.assertEqual(len(inspector.probe_calls), 1)

    def test_active_stream_failure_budget_resets_after_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(Path(tmp), notify=False)
            inspector = FakeInspector()
            notifier = FakeNotifier()
            runner = self._runner(config, inspector, notifier)
            try:
                runner.run_once()
                inspector.items = [
                    _surface_item(video_id="intermittent123", live_status="is_live")
                ]
                for _ in range(5):
                    inspector.outcome = ProbeOutcome(
                        ok=False,
                        access_class="probe_error",
                        error_kind="no_formats",
                        error_message="No video formats found",
                    )
                    runner.run_once()
                    inspector.outcome = ProbeOutcome(
                        ok=True,
                        access_class="members_only",
                        availability="subscriber_only",
                        live_status="is_live",
                    )
                    runner.run_once()
                with sqlite3.connect(runner.db_path) as connection:
                    failure_count, terminal_reason = connection.execute(
                        """
                        SELECT q.failure_count, i.terminal_reason
                        FROM probe_queue q
                        JOIN items i USING (origin_id, video_id)
                        WHERE q.video_id = 'intermittent123'
                        """
                    ).fetchone()
            finally:
                runner.close()

        self.assertEqual(failure_count, 0)
        self.assertIsNone(terminal_reason)
        self.assertEqual(len(inspector.probe_calls), 10)

    def test_post_live_surface_can_end_a_locked_member_stream(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(Path(tmp))
            inspector = FakeInspector()
            notifier = FakeNotifier()
            runner = self._runner(config, inspector, notifier)
            try:
                runner.run_once()
                inspector.items = [
                    _surface_item(video_id="locked12345", live_status="is_live")
                ]
                inspector.outcome = ProbeOutcome(
                    ok=True,
                    access_class="members_only",
                    availability="subscriber_only",
                    live_status="is_live",
                )
                runner.run_once()

                inspector.items = [
                    _surface_item(video_id="locked12345", live_status="post_live")
                ]
                inspector.outcome = ProbeOutcome(
                    ok=False,
                    access_class="members_only",
                    error_kind="members_only_denied",
                    error_message="Join this channel to get access",
                )
                runner.run_once()
            finally:
                runner.close()

        self.assertEqual(len(notifier.messages), 2)
        self.assertIn("会员直播结束", notifier.messages[-1]["text"])

    def test_upcoming_can_transition_directly_to_ended_between_polls(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(Path(tmp))
            inspector = FakeInspector()
            notifier = FakeNotifier()
            runner = self._runner(config, inspector, notifier)
            try:
                runner.run_once()
                inspector.items = [
                    _surface_item(video_id="missedlive123", live_status="is_upcoming")
                ]
                inspector.outcome = ProbeOutcome(
                    ok=True,
                    access_class="upcoming",
                    availability="subscriber_only",
                    live_status="is_upcoming",
                )
                runner.run_once()
                inspector.items = [
                    _surface_item(video_id="missedlive123", live_status="post_live")
                ]
                inspector.outcome = ProbeOutcome(
                    ok=True,
                    access_class="members_only",
                    availability="subscriber_only",
                    live_status="post_live",
                )
                runner.run_once()
            finally:
                runner.close()

        self.assertEqual(len(notifier.messages), 2)
        self.assertIn("会员预约", notifier.messages[0]["text"])
        self.assertIn("会员直播结束", notifier.messages[1]["text"])

    def test_youtube_cooldown_does_not_freeze_pending_telegram_outbox(self):
        class FailingNotifier:
            def send_text(self, *_args, **_kwargs):
                raise RuntimeError("temporary Telegram failure")

        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(Path(tmp))
            inspector = FakeInspector()
            runner = YoutubeMembershipDevRunner(
                config,
                inspector=inspector,
                notifier=FailingNotifier(),
                feed_fetcher=lambda _url: EMPTY_ATOM,
            )
            recovered_notifier = FakeNotifier()
            try:
                runner.run_once()
                inspector.items = [
                    _surface_item(video_id="cooldown123", live_status="is_upcoming")
                ]
                inspector.outcome = ProbeOutcome(
                    ok=True,
                    access_class="upcoming",
                    availability="subscriber_only",
                    live_status="is_upcoming",
                )
                runner.run_once()
                with sqlite3.connect(runner.db_path) as connection:
                    connection.execute(
                        "UPDATE notification_outbox SET available_at = ?",
                        ("2000-01-01T00:00:00+00:00",),
                    )
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO dev_state (key, value)
                        VALUES ('cooldown_until', '2999-01-01T00:00:00+00:00')
                        """
                    )
                    connection.commit()
                runner.notifier = recovered_notifier
                stats = runner.run_once()
            finally:
                runner.close()

        self.assertTrue(stats["cooldown"])
        self.assertEqual(stats["notifications_sent"], 1)
        self.assertEqual(len(recovered_notifier.messages), 1)

    def test_dev_runner_never_creates_a_job_in_main_state_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(Path(tmp), notify=False)
            main_store = Store(config.db_path)
            main_store.initialize()
            main_store.close()
            with sqlite3.connect(config.db_path) as connection:
                before = int(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])

            inspector = FakeInspector()
            notifier = FakeNotifier()
            runner = self._runner(config, inspector, notifier)
            try:
                runner.run_once()
                inspector.items = [_surface_item(video_id="isolated1234")]
                runner.run_once()
                dev_db_path = Path(str(runner.status()["db_path"]))
            finally:
                runner.close()

            with sqlite3.connect(config.db_path) as connection:
                after = int(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])

        self.assertEqual(after, before)
        self.assertNotEqual(dev_db_path.resolve(), config.db_path.resolve())


if __name__ == "__main__":
    unittest.main()
