from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ytb_tg_backup.dev.member_observer import (
    InspectionError,
    MemberObserver,
    ObservedChannel,
    ObserverConfig,
    ObserverStore,
    ProbeOutcome,
    SurfaceItem,
    YtDlpInspector,
    _probe_outcome_needs_retry,
    classify_probe_error,
    load_observer_config,
)


ATOM = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">
  <entry>
    <yt:videoId>public12345</yt:videoId>
    <title>Public stream</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=public12345" />
    <published>2026-07-22T10:00:00Z</published>
  </entry>
</feed>
"""

EMPTY_ATOM = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">
</feed>
"""

TWO_ITEM_ATOM = ATOM.replace(
    b"</feed>",
    b"""
  <entry>
    <yt:videoId>older123456</yt:videoId>
    <title>Older queued item</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=older123456" />
    <published>2026-07-22T09:00:00Z</published>
  </entry>
</feed>
""",
)


class FakeInspector:
    def __init__(self):
        self.items = [
            SurfaceItem(
                video_id="member12345",
                title="【メンバー限定】Member stream",
                url="https://www.youtube.com/watch?v=member12345",
            )
        ]
        self.probed: list[str] = []
        self.outcome = ProbeOutcome(
            ok=False,
            access_class="members_only",
            error_kind="members_only_denied",
            error_message="Join this channel to get access to members-only content.",
        )

    def list_tab(self, channel_id: str, tab: str) -> list[SurfaceItem]:
        return list(self.items)

    def list_members_playlist(self, channel_id: str) -> list[SurfaceItem]:
        return list(self.items)

    def probe(self, url: str) -> ProbeOutcome:
        self.probed.append(url)
        return self.outcome


class ChallengeInspector(FakeInspector):
    def __init__(self):
        super().__init__()
        self.items = []
        self.list_calls: list[str] = []

    def list_tab(self, channel_id: str, tab: str) -> list[SurfaceItem]:
        self.list_calls.append(tab)
        raise InspectionError("bot_check", "Sign in to confirm you're not a bot")


class SequencedInspector(FakeInspector):
    def __init__(self):
        super().__init__()
        self.items = []
        self.outcomes: list[ProbeOutcome] = []

    def probe(self, url: str) -> ProbeOutcome:
        self.probed.append(url)
        return self.outcomes.pop(0)


class RoutingInspector(FakeInspector):
    def __init__(self):
        super().__init__()
        self.items = []

    def probe(self, url: str) -> ProbeOutcome:
        self.probed.append(url)
        if url.endswith("public12345"):
            return ProbeOutcome(
                ok=False,
                access_class="probe_error",
                error_kind="bot_check",
                error_message="confirm you're not a bot",
            )
        return ProbeOutcome(ok=True, access_class="accessible")


class MemberObserverTest(unittest.TestCase):
    def test_error_classification_distinguishes_membership_and_challenges(self):
        self.assertEqual(
            classify_probe_error("Join this channel to get access to members-only content"),
            "members_only_denied",
        )
        self.assertEqual(classify_probe_error("HTTP Error 429: Too Many Requests"), "rate_limited")
        self.assertEqual(classify_probe_error("Sign in to confirm you're not a bot"), "bot_check")
        self.assertEqual(classify_probe_error("A PO Token is required"), "po_token_required")
        self.assertEqual(classify_probe_error("This live event will begin in 10 hours"), "upcoming")
        self.assertEqual(
            classify_probe_error(
                "Video unavailable. This video has been removed by the uploader. "
                "No video formats found"
            ),
            "removed",
        )
        self.assertEqual(
            classify_probe_error("Private video; no video formats found"),
            "private",
        )
        self.assertEqual(
            classify_probe_error("This video is unavailable; no video formats found"),
            "unavailable",
        )

    def test_config_resolves_paths_and_rejects_all_authentication(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "observer.toml"
            config_text = """
[observer]
data_dir = "work/observer"
yt_dlp = ".venv/bin/yt-dlp"
anonymous_tabs = ["streams", "videos"]

[[channels]]
id = "test"
channel_id = "UC1234567890123456789012"
""".strip()
            path.write_text(config_text, encoding="utf-8")
            config = load_observer_config(path)
            self.assertEqual(config.data_dir, (Path(tmp) / "work/observer").resolve())
            self.assertEqual(config.yt_dlp, str((Path(tmp) / ".venv/bin/yt-dlp").resolve()))
            self.assertEqual(config.anonymous_tabs, ["streams", "videos"])

            path.write_text(
                config_text + "\n\n[auth]\nenabled = true\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "anonymous-only"):
                load_observer_config(path)

            path.write_text(
                config_text
                + '\n\n[auth]\nenabled = false\nextra_args = ["--cookies", "/tmp/cookies.txt"]\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "cookies are not supported"):
                load_observer_config(path)

            path.write_text(
                config_text + "\n\n[auth]\nenabled = false\nextra_args = []\n",
                encoding="utf-8",
            )
            self.assertEqual(load_observer_config(path).anonymous_tabs, ["streams", "videos"])

    def test_config_rejects_duplicate_enabled_channel_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "observer.toml"
            path.write_text(
                """
[[channels]]
id = "one"
channel_id = "UC1234567890123456789012"

[[channels]]
id = "two"
channel_id = "UC1234567890123456789012"
""".strip(),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "channel_id values must be unique"):
                load_observer_config(path)

    def test_baseline_probes_keyword_matches_and_records_atom_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = ObserverConfig(
                path=Path(tmp) / "observer.toml",
                data_dir=Path(tmp) / "data",
                yt_dlp="yt-dlp",
                poll_interval_seconds=300,
                request_timeout_seconds=30,
                request_spacing_seconds=0,
                tab_limit=20,
                seed_probe_keyword_limit_per_channel=5,
                anonymous_tabs=["streams"],
                anonymous_members_playlist=False,
                stop_at=None,
                log_level="INFO",
                channels=[
                    ObservedChannel(
                        id="channel",
                        name="Channel",
                        channel_id="UC1234567890123456789012",
                    )
                ],
            )
            inspector = FakeInspector()
            observer = MemberObserver(config, feed_fetcher=lambda _url: ATOM, inspector=inspector)
            try:
                stats = observer.run_cycle()
                report = observer.store.report()
            finally:
                observer.close()

            self.assertEqual(stats["probe_count"], 1)
            self.assertEqual(stats["members_only_count"], 1)
            self.assertEqual(len(inspector.probed), 1)
            self.assertEqual(report["probe_class_counts"], {"anon:members_only": 1})
            self.assertEqual(report["members_only"][0]["atom_seen_ever"], 0)

    def test_new_non_keyword_item_is_probed_after_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = ObserverConfig(
                path=Path(tmp) / "observer.toml",
                data_dir=Path(tmp) / "data",
                yt_dlp="yt-dlp",
                poll_interval_seconds=300,
                request_timeout_seconds=30,
                request_spacing_seconds=0,
                tab_limit=20,
                seed_probe_keyword_limit_per_channel=0,
                anonymous_tabs=["streams"],
                anonymous_members_playlist=False,
                stop_at=None,
                log_level="INFO",
                channels=[ObservedChannel("channel", "Channel", "UC1234567890123456789012")],
            )
            inspector = FakeInspector()
            inspector.items = [SurfaceItem("old12345678", "No marker", "https://youtu.be/old12345678")]
            observer = MemberObserver(config, feed_fetcher=lambda _url: ATOM, inspector=inspector)
            try:
                observer.run_cycle()
                self.assertEqual(inspector.probed, [])
                inspector.items.insert(
                    0,
                    SurfaceItem("new12345678", "Still no marker", "https://youtu.be/new12345678"),
                )
                stats = observer.run_cycle()
            finally:
                observer.close()

            self.assertEqual(stats["probe_count"], 1)
            self.assertTrue(inspector.probed[0].endswith("new12345678"))

    def test_upcoming_probe_is_repeated_until_state_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = ObserverConfig(
                path=Path(tmp) / "observer.toml",
                data_dir=Path(tmp) / "data",
                yt_dlp="yt-dlp",
                poll_interval_seconds=300,
                request_timeout_seconds=30,
                request_spacing_seconds=0,
                tab_limit=20,
                seed_probe_keyword_limit_per_channel=5,
                anonymous_tabs=["streams"],
                anonymous_members_playlist=False,
                stop_at=None,
                log_level="INFO",
                channels=[ObservedChannel("channel", "Channel", "UC1234567890123456789012")],
            )
            inspector = FakeInspector()
            inspector.outcome = ProbeOutcome(
                ok=False,
                access_class="upcoming",
                error_kind="upcoming",
                error_message="This live event will begin in 10 hours.",
            )
            observer = MemberObserver(config, feed_fetcher=lambda _url: ATOM, inspector=inspector)
            try:
                first = observer.run_cycle()
                inspector.items = []
                second = observer.run_cycle()
            finally:
                observer.close()

            self.assertEqual(first["probe_count"], 1)
            self.assertEqual(second["probe_count"], 1)
            self.assertEqual(len(inspector.probed), 2)

    def test_report_preserves_historical_member_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = ObserverConfig(
                path=Path(tmp) / "observer.toml",
                data_dir=Path(tmp) / "data",
                yt_dlp="yt-dlp",
                poll_interval_seconds=300,
                request_timeout_seconds=30,
                request_spacing_seconds=0,
                tab_limit=20,
                seed_probe_keyword_limit_per_channel=5,
                anonymous_tabs=["streams"],
                anonymous_members_playlist=False,
                stop_at=None,
                log_level="INFO",
                channels=[ObservedChannel("channel", "Channel", "UC1234567890123456789012")],
            )
            inspector = FakeInspector()
            observer = MemberObserver(config, feed_fetcher=lambda _url: ATOM, inspector=inspector)
            try:
                observer.run_cycle()
                cycle_id = observer.store.begin_cycle(1)
                observer.store.record_probe(
                    cycle_id=cycle_id,
                    channel_id=config.channels[0].channel_id,
                    video_id="member12345",
                    mode="anon",
                    atom_seen_this_cycle=False,
                    outcome=ProbeOutcome(ok=True, access_class="accessible"),
                )
                observer.store.finish_cycle(
                    cycle_id,
                    {
                        "surface_errors": 0,
                        "item_sightings": 0,
                        "probe_count": 1,
                        "members_only_count": 0,
                        "probe_errors": 0,
                        "surface_skips": 0,
                        "interrupted": 0,
                    },
                )
                report = observer.store.report()
            finally:
                observer.close()

            self.assertEqual(len(report["members_only"]), 1)
            self.assertEqual(report["members_only"][0]["access_class"], "members_only")
            self.assertEqual(report["members_only"][0]["current_access_class"], "accessible")
            self.assertEqual(report["channel_coverage"][0]["probe_confirmed_member_items"], 1)

    def test_transient_probe_error_is_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = ObserverConfig(
                path=Path(tmp) / "observer.toml",
                data_dir=Path(tmp) / "data",
                yt_dlp="yt-dlp",
                poll_interval_seconds=300,
                request_timeout_seconds=30,
                request_spacing_seconds=0,
                tab_limit=20,
                seed_probe_keyword_limit_per_channel=5,
                anonymous_tabs=["streams"],
                anonymous_members_playlist=False,
                stop_at=None,
                log_level="INFO",
                channels=[ObservedChannel("channel", "Channel", "UC1234567890123456789012")],
            )
            inspector = FakeInspector()
            inspector.outcome = ProbeOutcome(
                ok=False,
                access_class="probe_error",
                error_kind="timeout",
                error_message="yt-dlp timed out",
            )
            observer = MemberObserver(config, feed_fetcher=lambda _url: ATOM, inspector=inspector)
            try:
                first = observer.run_cycle()
                second = observer.run_cycle()
            finally:
                observer.close()

            self.assertEqual(first["probe_errors"], 1)
            self.assertEqual(second["probe_count"], 1)
            self.assertEqual(len(inspector.probed), 2)

    def test_unattempted_candidates_survive_challenge_and_disappearance(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = ObserverConfig(
                path=Path(tmp) / "observer.toml",
                data_dir=Path(tmp) / "data",
                yt_dlp="yt-dlp",
                poll_interval_seconds=300,
                request_timeout_seconds=30,
                request_spacing_seconds=0,
                tab_limit=20,
                seed_probe_keyword_limit_per_channel=5,
                anonymous_tabs=["streams"],
                anonymous_members_playlist=False,
                stop_at=None,
                log_level="INFO",
                channels=[ObservedChannel("channel", "Channel", "UC1234567890123456789012")],
                retry_probe_limit_per_channel=1,
            )
            inspector = SequencedInspector()
            observer = MemberObserver(config, feed_fetcher=lambda _url: ATOM, inspector=inspector)
            try:
                observer.run_cycle()
                inspector.items = [
                    SurfaceItem("first123456", "No marker one", "https://youtu.be/first123456"),
                    SurfaceItem("second12345", "No marker two", "https://youtu.be/second12345"),
                ]
                inspector.outcomes = [
                    ProbeOutcome(
                        ok=False,
                        access_class="probe_error",
                        error_kind="bot_check",
                        error_message="confirm you're not a bot",
                    )
                ]
                challenged = observer.run_cycle()
                queued_after_challenge = observer.store.conn.execute(
                    "SELECT COUNT(*) FROM probe_queue"
                ).fetchone()[0]

                observer.request_paused_until = None
                observer.store.delete_state("request_paused_until")
                observer.store.delete_state("request_pause_reason")
                inspector.items = []
                inspector.outcomes = [
                    ProbeOutcome(ok=True, access_class="accessible"),
                ]
                recovered = observer.run_cycle()
                queued_after_first_recovery = observer.store.conn.execute(
                    "SELECT COUNT(*) FROM probe_queue"
                ).fetchone()[0]
                inspector.outcomes = [ProbeOutcome(ok=True, access_class="accessible")]
                final_recovery = observer.run_cycle()
                queued_after_recovery = observer.store.conn.execute(
                    "SELECT COUNT(*) FROM probe_queue"
                ).fetchone()[0]
            finally:
                observer.close()

            self.assertEqual(challenged["probe_count"], 1)
            self.assertEqual(queued_after_challenge, 2)
            self.assertEqual(recovered["probe_count"], 1)
            self.assertEqual(queued_after_first_recovery, 1)
            self.assertEqual(final_recovery["probe_count"], 1)
            self.assertEqual(queued_after_recovery, 0)
            self.assertEqual(
                [url.rsplit("/", 1)[-1] for url in inspector.probed],
                ["first123456", "second12345", "first123456"],
            )

    def test_old_queue_item_runs_before_fresh_atom_challenge(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = ObserverConfig(
                path=Path(tmp) / "observer.toml",
                data_dir=Path(tmp) / "data",
                yt_dlp="yt-dlp",
                poll_interval_seconds=300,
                request_timeout_seconds=30,
                request_spacing_seconds=0,
                tab_limit=20,
                seed_probe_keyword_limit_per_channel=0,
                anonymous_tabs=["streams"],
                anonymous_members_playlist=False,
                stop_at=None,
                log_level="INFO",
                channels=[ObservedChannel("channel", "Channel", "UC1234567890123456789012")],
                retry_probe_limit_per_channel=1,
            )
            inspector = RoutingInspector()
            feed = [EMPTY_ATOM]
            observer = MemberObserver(
                config,
                feed_fetcher=lambda _url: feed[0],
                inspector=inspector,
            )
            queued_item = SurfaceItem(
                "older123456",
                "Older queued item",
                "https://youtu.be/older123456",
            )
            try:
                observer.run_cycle()
                observer.store.record_item(
                    cycle_id=1,
                    channel=config.channels[0],
                    surface="atom",
                    item=queued_item,
                    observed_at="2026-07-22T00:00:00+00:00",
                )
                observer.store.record_probe(
                    cycle_id=1,
                    channel_id=config.channels[0].channel_id,
                    video_id=queued_item.video_id,
                    mode="anon",
                    atom_seen_this_cycle=True,
                    outcome=ProbeOutcome(
                        ok=False,
                        access_class="probe_error",
                        error_kind="timeout",
                        error_message="timed out",
                    ),
                )
                observer.store.queue_probe(
                    config.channels[0].channel_id,
                    queued_item.video_id,
                    "anon",
                    reason="test",
                )
                feed[0] = TWO_ITEM_ATOM
                stats = observer.run_cycle()
                queued_ids = [
                    row[0]
                    for row in observer.store.conn.execute(
                        "SELECT video_id FROM probe_queue ORDER BY video_id"
                    )
                ]
            finally:
                observer.close()

            self.assertEqual(stats["probe_count"], 2)
            self.assertTrue(inspector.probed[0].endswith("older123456"))
            self.assertTrue(inspector.probed[1].endswith("public12345"))
            self.assertEqual(queued_ids, ["public12345"])

    def test_anonymous_challenge_stops_requests_and_persists_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = ObserverConfig(
                path=Path(tmp) / "observer.toml",
                data_dir=Path(tmp) / "data",
                yt_dlp="yt-dlp",
                poll_interval_seconds=300,
                request_timeout_seconds=30,
                request_spacing_seconds=0,
                tab_limit=20,
                seed_probe_keyword_limit_per_channel=0,
                anonymous_tabs=["streams", "videos"],
                anonymous_members_playlist=False,
                stop_at=None,
                log_level="INFO",
                channels=[ObservedChannel("channel", "Channel", "UC1234567890123456789012")],
            )
            inspector = ChallengeInspector()
            observer = MemberObserver(config, feed_fetcher=lambda _url: ATOM, inspector=inspector)
            try:
                first = observer.run_cycle()
                first_report = observer.store.report()
            finally:
                observer.close()

            self.assertEqual(inspector.list_calls, ["streams"])
            self.assertEqual(first["surface_errors"], 1)
            self.assertEqual(first["surface_skips"], 1)
            self.assertEqual(first_report["latest_cycle"]["success"], 0)
            self.assertIn("request_paused_until", first_report["observer_state"])

            second_inspector = ChallengeInspector()
            observer = MemberObserver(config, feed_fetcher=lambda _url: ATOM, inspector=second_inspector)
            try:
                second = observer.run_cycle()
                rows = observer.store.conn.execute(
                    "SELECT status, COUNT(*) AS count FROM surface_runs "
                    "WHERE cycle_id = (SELECT MAX(id) FROM cycles) GROUP BY status"
                ).fetchall()
            finally:
                observer.close()

            self.assertEqual(second_inspector.list_calls, [])
            self.assertEqual(second["surface_skips"], 3)
            self.assertEqual({row["status"]: row["count"] for row in rows}, {"skipped": 3})

    def test_atom_challenge_stops_all_following_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = ObserverConfig(
                path=Path(tmp) / "observer.toml",
                data_dir=Path(tmp) / "data",
                yt_dlp="yt-dlp",
                poll_interval_seconds=300,
                request_timeout_seconds=30,
                request_spacing_seconds=0,
                tab_limit=20,
                seed_probe_keyword_limit_per_channel=0,
                anonymous_tabs=["streams"],
                anonymous_members_playlist=False,
                stop_at=None,
                log_level="INFO",
                channels=[ObservedChannel("channel", "Channel", "UC1234567890123456789012")],
            )
            inspector = ChallengeInspector()

            def rate_limited_feed(_url: str) -> bytes:
                raise RuntimeError("HTTP Error 429: Too Many Requests")

            observer = MemberObserver(config, feed_fetcher=rate_limited_feed, inspector=inspector)
            try:
                stats = observer.run_cycle()
                state = observer.store.report()["observer_state"]
            finally:
                observer.close()

            self.assertEqual(inspector.list_calls, [])
            self.assertEqual(stats["surface_errors"], 1)
            self.assertEqual(stats["surface_skips"], 1)
            self.assertIn("request_paused_until", state)

    def test_interruption_records_every_expected_surface(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = ObserverConfig(
                path=Path(tmp) / "observer.toml",
                data_dir=Path(tmp) / "data",
                yt_dlp="yt-dlp",
                poll_interval_seconds=300,
                request_timeout_seconds=30,
                request_spacing_seconds=0,
                tab_limit=20,
                seed_probe_keyword_limit_per_channel=0,
                anonymous_tabs=["streams", "videos"],
                anonymous_members_playlist=False,
                stop_at=None,
                log_level="INFO",
                channels=[
                    ObservedChannel("one", "One", "UC1234567890123456789012"),
                    ObservedChannel("two", "Two", "UC2234567890123456789012"),
                ],
            )
            inspector = FakeInspector()
            inspector.items = []
            observer = MemberObserver(config, feed_fetcher=lambda _url: ATOM, inspector=inspector)

            def stopping_list(_channel_id: str, _tab: str):
                observer.stop_event.set()
                return []

            inspector.list_tab = stopping_list
            try:
                stats = observer.run_cycle()
                rows = observer.store.conn.execute(
                    "SELECT surface, status FROM surface_runs "
                    "WHERE cycle_id = (SELECT MAX(id) FROM cycles)"
                ).fetchall()
                success = observer.store.conn.execute(
                    "SELECT success FROM cycles ORDER BY id DESC LIMIT 1"
                ).fetchone()[0]
            finally:
                observer.close()

            self.assertEqual(stats["interrupted"], 1)
            self.assertEqual(stats["surface_skips"], 4)
            self.assertEqual(len(rows), 6)
            self.assertEqual(success, 0)

    def test_probe_keeps_metadata_when_formats_are_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = ObserverConfig(
                path=Path(tmp) / "observer.toml",
                data_dir=Path(tmp) / "data",
                yt_dlp="yt-dlp",
                poll_interval_seconds=300,
                request_timeout_seconds=30,
                request_spacing_seconds=0,
                tab_limit=20,
                seed_probe_keyword_limit_per_channel=0,
                anonymous_tabs=["streams"],
                anonymous_members_playlist=False,
                stop_at=None,
                log_level="INFO",
                channels=[ObservedChannel("channel", "Channel", "UC1234567890123456789012")],
            )
            payload = json.dumps(
                {
                    "id": "member12345",
                    "title": "Member video",
                    "availability": "subscriber_only",
                    "formats": [],
                }
            )
            completed = subprocess.CompletedProcess([], 0, payload, "WARNING: no video formats found")
            inspector = YtDlpInspector(config)
            with patch("ytb_tg_backup.dev.member_observer.subprocess.run", return_value=completed) as run:
                outcome = inspector.probe("https://youtu.be/member12345")

            command = run.call_args.args[0]
            self.assertIn("--ignore-config", command)
            self.assertIn("--ignore-no-formats-error", command)
            self.assertNotIn("--cookies", command)
            self.assertNotIn("--cookies-from-browser", command)
            self.assertNotIn("--no-warnings", command)
            self.assertEqual(outcome.access_class, "members_only")
            self.assertEqual(outcome.metadata["title"], "Member video")

            removed = subprocess.CompletedProcess(
                [],
                0,
                json.dumps({"id": "removed123", "formats": []}),
                "WARNING: Video unavailable. This video has been removed by the uploader. "
                "WARNING: No video formats found",
            )
            with patch(
                "ytb_tg_backup.dev.member_observer.subprocess.run",
                return_value=removed,
            ):
                removed_outcome = inspector.probe("https://youtu.be/removed123")

            self.assertFalse(removed_outcome.ok)
            self.assertEqual(removed_outcome.access_class, "removed")
            self.assertEqual(removed_outcome.error_kind, "removed")
            self.assertFalse(_probe_outcome_needs_retry(removed_outcome))

    def test_flat_surface_fails_on_critical_success_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = ObserverConfig(
                path=Path(tmp) / "observer.toml",
                data_dir=Path(tmp) / "data",
                yt_dlp="yt-dlp",
                poll_interval_seconds=300,
                request_timeout_seconds=30,
                request_spacing_seconds=0,
                tab_limit=20,
                seed_probe_keyword_limit_per_channel=0,
                anonymous_tabs=["streams"],
                anonymous_members_playlist=False,
                stop_at=None,
                log_level="INFO",
                channels=[ObservedChannel("channel", "Channel", "UC1234567890123456789012")],
            )
            completed = subprocess.CompletedProcess(
                [],
                0,
                json.dumps({"entries": []}),
                "WARNING: A PO Token is required",
            )
            inspector = YtDlpInspector(config)
            with patch("ytb_tg_backup.dev.member_observer.subprocess.run", return_value=completed) as run:
                with self.assertRaisesRegex(InspectionError, "PO Token"):
                    inspector.list_tab(config.channels[0].channel_id, "streams")

            self.assertIn("--ignore-config", run.call_args.args[0])
            self.assertNotIn("--cookies", run.call_args.args[0])
            self.assertNotIn("--no-warnings", run.call_args.args[0])

    def test_storyboard_does_not_turn_anonymous_probe_into_member_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = ObserverConfig(
                path=Path(tmp) / "observer.toml",
                data_dir=Path(tmp) / "data",
                yt_dlp="yt-dlp",
                poll_interval_seconds=300,
                request_timeout_seconds=30,
                request_spacing_seconds=0,
                tab_limit=20,
                seed_probe_keyword_limit_per_channel=0,
                anonymous_tabs=["streams"],
                anonymous_members_playlist=False,
                stop_at=None,
                log_level="INFO",
                channels=[ObservedChannel("channel", "Channel", "UC1234567890123456789012")],
            )
            payload = json.dumps(
                {
                    "id": "member12345",
                    "title": "Member video",
                    "availability": "subscriber_only",
                    "formats": [
                        {
                            "format_id": "sb0",
                            "url": "https://example.invalid/storyboard.mhtml",
                            "vcodec": "none",
                            "acodec": "none",
                        }
                    ],
                }
            )
            completed = subprocess.CompletedProcess([], 0, payload, "WARNING: A PO Token is required")
            inspector = YtDlpInspector(config)
            with patch(
                "ytb_tg_backup.dev.member_observer.subprocess.run",
                return_value=completed,
            ) as run:
                outcome = inspector.probe("https://youtu.be/member12345")

            self.assertTrue(outcome.ok)
            self.assertEqual(outcome.access_class, "members_only")
            self.assertNotEqual(outcome.access_class, "members_only_accessible")
            self.assertIn("--ignore-config", run.call_args.args[0])
            self.assertNotIn("--cookies", run.call_args.args[0])

    def test_upcoming_metadata_remains_in_retryable_lifecycle_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = ObserverConfig(
                path=Path(tmp) / "observer.toml",
                data_dir=Path(tmp) / "data",
                yt_dlp="yt-dlp",
                poll_interval_seconds=300,
                request_timeout_seconds=30,
                request_spacing_seconds=0,
                tab_limit=20,
                seed_probe_keyword_limit_per_channel=0,
                anonymous_tabs=["streams"],
                anonymous_members_playlist=False,
                stop_at=None,
                log_level="INFO",
                channels=[ObservedChannel("channel", "Channel", "UC1234567890123456789012")],
            )
            payload = json.dumps(
                {
                    "id": "upcoming123",
                    "title": "Members-Only upcoming stream",
                    "availability": "public",
                    "live_status": "is_upcoming",
                    "formats": [],
                }
            )
            completed = subprocess.CompletedProcess([], 0, payload, "WARNING: no video formats found")
            inspector = YtDlpInspector(config)
            with patch("ytb_tg_backup.dev.member_observer.subprocess.run", return_value=completed):
                outcome = inspector.probe("https://youtu.be/upcoming123")

            self.assertTrue(outcome.ok)
            self.assertEqual(outcome.access_class, "upcoming")
            self.assertEqual(outcome.live_status, "is_upcoming")

    def test_generic_no_formats_is_terminal(self):
        self.assertFalse(
            _probe_outcome_needs_retry(
                ProbeOutcome(
                    ok=False,
                    access_class="probe_error",
                    error_kind="no_formats",
                    error_message="No video formats found",
                )
            )
        )

    def test_existing_cycles_table_is_migrated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "observer.db"
            conn = sqlite3.connect(path)
            conn.execute(
                """
                CREATE TABLE cycles (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  started_at TEXT NOT NULL,
                  finished_at TEXT,
                  success INTEGER,
                  channel_count INTEGER NOT NULL,
                  surface_errors INTEGER NOT NULL DEFAULT 0,
                  item_sightings INTEGER NOT NULL DEFAULT 0,
                  probe_count INTEGER NOT NULL DEFAULT 0,
                  members_only_count INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.commit()
            conn.close()

            store = ObserverStore(path)
            try:
                columns = {
                    row["name"] for row in store.conn.execute("PRAGMA table_info(cycles)")
                }
            finally:
                store.close()

            self.assertTrue({"interrupted", "probe_errors", "surface_skips"} <= columns)

    def test_report_uses_latest_completed_cycle_and_read_only_connection(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "observer.db"
            store = ObserverStore(path)
            try:
                completed_id = store.begin_cycle(1)
                store.finish_cycle(
                    completed_id,
                    {
                        "surface_errors": 0,
                        "item_sightings": 0,
                        "probe_count": 0,
                        "members_only_count": 0,
                        "probe_errors": 0,
                        "surface_skips": 0,
                        "interrupted": 0,
                    },
                )
                active_id = store.begin_cycle(1)
            finally:
                store.close()

            read_only = ObserverStore(path, read_only=True)
            try:
                report = read_only.report()
                with self.assertRaises(sqlite3.OperationalError):
                    read_only.conn.execute("CREATE TABLE should_fail (id INTEGER)")
            finally:
                read_only.close()

            self.assertEqual(report["latest_cycle"]["id"], completed_id)
            self.assertEqual(report["active_cycle"]["id"], active_id)
            self.assertEqual(report["totals"]["unfinished_cycles"], 1)


if __name__ == "__main__":
    unittest.main()
