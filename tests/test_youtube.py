from datetime import datetime, timedelta, timezone
import json
import subprocess
import time
import unittest
from unittest import mock

from ytb_tg_backup.extension_api import RouteLease
from ytb_tg_backup.network import ConnectionRuntime, NetworkScope
from ytb_tg_backup.youtube import (
    list_recent_channel_videos,
    resolve_channel_id,
    youtube_channel_feed_url,
)


CHANNEL_ID = "UCabcdefghij1234567890"
HOUR = 3600


def hours_ago(hours):
    return datetime.now(timezone.utc) - timedelta(hours=hours)


def tab_entry(video_id, *, age_hours=None, live_status=None, availability=None):
    return {
        "_type": "url",
        "ie_key": "Youtube",
        "id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "title": f"ASMR {video_id}",
        "timestamp": None if age_hours is None else int(time.time() - age_hours * HOUR),
        "live_status": live_status,
        "availability": availability,
    }


def fake_tab_listing(**tabs):
    """Return a subprocess.run stand-in that serves yt-dlp JSON per channel tab."""

    def run(cmd, **_kwargs):
        tab = cmd[-1].rsplit("/", 1)[-1]
        if tab not in tabs:
            return subprocess.CompletedProcess(
                cmd,
                1,
                "",
                f"ERROR: [youtube:tab] {CHANNEL_ID}: This channel does not have a {tab} tab\n",
            )
        payload = {"_type": "playlist", "id": CHANNEL_ID, "entries": tabs[tab]}
        return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")

    return run


class _RecordingProxyPolicy:
    def __init__(self):
        self.requests = []

    def acquire(self, request):
        self.requests.append(request)
        return RouteLease(route_id="proxy-a", mode="proxy", proxy_url="http://proxy.example:8080")

    def report(self, request, lease, outcome):
        return None


class YouTubeFallbackListingTest(unittest.TestCase):
    def test_keeps_live_and_entries_since_boundary_newest_first(self):
        listing = fake_tab_listing(
            streams=[
                # Channel tabs give upcoming placeholders no time; one with a
                # future time is dropped as well.
                tab_entry("upcoming1", live_status="is_upcoming"),
                tab_entry("upcoming2", age_hours=-2, live_status="is_upcoming"),
                tab_entry("livenow1", live_status="is_live"),
                tab_entry("stream2h", age_hours=2, live_status="was_live"),
                tab_entry("members1", age_hours=1, live_status="was_live", availability="subscriber_only"),
                tab_entry("undated1", live_status="was_live"),
                tab_entry("stream5d", age_hours=5 * 24, live_status="was_live"),
            ],
            videos=[
                tab_entry("video5h", age_hours=5),
                tab_entry("stream2h", age_hours=2),
                tab_entry("video7h", age_hours=7),
                tab_entry("video30h", age_hours=30),
                tab_entry("video1y", age_hours=365 * 24),
            ],
        )
        with mock.patch("ytb_tg_backup.youtube.subprocess.run", side_effect=listing) as run:
            items = list_recent_channel_videos(
                CHANNEL_ID,
                hours_ago(6),
                ["/opt/yt-dlp"],
                ConnectionRuntime(),
            )

        self.assertEqual(
            [item.external_id for item in items],
            ["livenow1", "stream2h", "video5h"],
        )
        for item in items:
            self.assertEqual((item.provider, item.content_kind), ("youtube", "video"))
            self.assertEqual(item.url, f"https://www.youtube.com/watch?v={item.external_id}")
            self.assertEqual(item.title, f"ASMR {item.external_id}")
            self.assertIsNone(item.published_at)
            self.assertIsNone(item.live_status)
            self.assertEqual(item.metadata, {})

        self.assertEqual(
            [call.args[0][-1] for call in run.call_args_list],
            [
                f"https://www.youtube.com/channel/{CHANNEL_ID}/streams",
                f"https://www.youtube.com/channel/{CHANNEL_ID}/videos",
            ],
        )
        for call in run.call_args_list:
            cmd = call.args[0]
            self.assertEqual(cmd[0], "/opt/yt-dlp")
            self.assertIn("--ignore-config", cmd)
            self.assertIn("--flat-playlist", cmd)
            self.assertIn("--dump-single-json", cmd)
            self.assertEqual(cmd[cmd.index("--playlist-end") + 1], "15")
            self.assertEqual(cmd[cmd.index("--extractor-args") + 1], "youtubetab:approximate_date")
            self.assertNotIn("--download-archive", cmd)
            self.assertEqual(call.kwargs["timeout"], 30)

    def test_missing_channel_tab_is_treated_as_empty(self):
        listing = fake_tab_listing(videos=[tab_entry("video1h", age_hours=1)])
        with mock.patch("ytb_tg_backup.youtube.subprocess.run", side_effect=listing) as run:
            items = list_recent_channel_videos(CHANNEL_ID, hours_ago(10), ["yt-dlp"], ConnectionRuntime())

        self.assertEqual([item.external_id for item in items], ["video1h"])
        self.assertEqual(run.call_count, 2)

    def test_listing_uses_source_discovery_route_for_each_tab(self):
        policy = _RecordingProxyPolicy()
        listing = fake_tab_listing(streams=[], videos=[])
        with (
            mock.patch.dict("os.environ", {"HTTPS_PROXY": "http://inherited.example:3128"}),
            mock.patch("ytb_tg_backup.youtube.subprocess.run", side_effect=listing) as run,
        ):
            list_recent_channel_videos(CHANNEL_ID, hours_ago(10), ["yt-dlp"], ConnectionRuntime(policy))

        self.assertEqual(
            [(request.scope, request.provider, request.target_url) for request in policy.requests],
            [
                (
                    NetworkScope.SOURCE_DISCOVERY,
                    "youtube",
                    f"https://www.youtube.com/channel/{CHANNEL_ID}/streams",
                ),
                (
                    NetworkScope.SOURCE_DISCOVERY,
                    "youtube",
                    f"https://www.youtube.com/channel/{CHANNEL_ID}/videos",
                ),
            ],
        )
        for call in run.call_args_list:
            cmd = call.args[0]
            self.assertEqual(cmd[-3:-1], ["--proxy", "http://proxy.example:8080"])
            self.assertNotIn("HTTPS_PROXY", call.kwargs["env"])

    def test_failed_listing_raises_with_yt_dlp_error(self):
        failed = subprocess.CompletedProcess(
            [],
            1,
            "",
            "ERROR: [youtube:tab] UCabcdefghij1234567890: HTTP Error 429: Too Many Requests\n",
        )
        with mock.patch("ytb_tg_backup.youtube.subprocess.run", return_value=failed):
            with self.assertRaisesRegex(RuntimeError, "/streams listing exited with 1: .*HTTP Error 429"):
                list_recent_channel_videos(CHANNEL_ID, hours_ago(10), ["yt-dlp"], ConnectionRuntime())


class YouTubeTest(unittest.TestCase):
    def test_youtube_channel_feed_url(self):
        self.assertEqual(
            youtube_channel_feed_url("UC123"),
            "https://www.youtube.com/feeds/videos.xml?channel_id=UC123",
        )

    def test_resolve_channel_id_keeps_uc_id(self):
        channel_id = "UCabcdefghij1234567890"
        self.assertEqual(resolve_channel_id(channel_id, "yt-dlp"), channel_id)

    def test_resolve_channel_id_uses_yt_dlp_for_handle(self):
        completed = subprocess.CompletedProcess(
            ["yt-dlp"],
            0,
            stdout="UCabcdefghij1234567890\n",
            stderr="",
        )
        with (
            mock.patch("ytb_tg_backup.youtube._resolve_channel_id_from_html", return_value=None),
            mock.patch("ytb_tg_backup.youtube.subprocess.run", return_value=completed) as run,
        ):
            self.assertEqual(resolve_channel_id("@rnqqU", "/opt/yt-dlp"), "UCabcdefghij1234567890")

        args = run.call_args.args[0]
        self.assertEqual(args[0], "/opt/yt-dlp")
        self.assertEqual(args[-1], "https://www.youtube.com/@rnqqU")

    def test_resolve_channel_id_prefers_html_feed_link(self):
        with (
            mock.patch("ytb_tg_backup.youtube._resolve_channel_id_from_html", return_value="UCabcdefghij1234567890"),
            mock.patch("ytb_tg_backup.youtube.subprocess.run") as run,
        ):
            self.assertEqual(resolve_channel_id("@rnqqU", "/opt/yt-dlp"), "UCabcdefghij1234567890")
        run.assert_not_called()

    def test_resolve_channel_id_accepts_canonical_handle_url(self):
        completed = subprocess.CompletedProcess(
            ["yt-dlp"],
            0,
            stdout="UCabcdefghij1234567890\n",
            stderr="",
        )
        with (
            mock.patch("ytb_tg_backup.youtube._resolve_channel_id_from_html", return_value=None) as html,
            mock.patch("ytb_tg_backup.youtube.subprocess.run", return_value=completed) as run,
        ):
            self.assertEqual(
                resolve_channel_id("https://youtube.com/@rnqqU/", "/opt/yt-dlp"),
                "UCabcdefghij1234567890",
            )

        html.assert_called_once_with("https://www.youtube.com/@rnqqU")
        self.assertEqual(run.call_args.args[0][-1], "https://www.youtube.com/@rnqqU")

    def test_resolve_channel_id_extracts_canonical_channel_url_without_request(self):
        channel_id = "UCabcdefghij1234567890"
        with (
            mock.patch("ytb_tg_backup.youtube._resolve_channel_id_from_html") as html,
            mock.patch("ytb_tg_backup.youtube.subprocess.run") as run,
        ):
            self.assertEqual(
                resolve_channel_id(f"https://www.youtube.com/channel/{channel_id}", "yt-dlp"),
                channel_id,
            )
        html.assert_not_called()
        run.assert_not_called()

    def test_resolve_channel_id_rejects_non_youtube_and_noncanonical_urls_before_request(self):
        invalid_refs = (
            "https://example.com/@rnqqU",
            "https://www.youtube.com.example.com/@rnqqU",
            "https://www.youtube.com@127.0.0.1/@rnqqU",
            "https://www.youtube.com:443/@rnqqU",
            "http://www.youtube.com/@rnqqU",
            "https://www.youtube.com/redirect?q=http://127.0.0.1",
            "https://www.youtube.com/@rnqqU?next=http://127.0.0.1",
            "https://www.youtube.com/watch?v=abc123",
        )
        with (
            mock.patch("ytb_tg_backup.youtube._resolve_channel_id_from_html") as html,
            mock.patch("ytb_tg_backup.youtube.subprocess.run") as run,
        ):
            for channel_ref in invalid_refs:
                with self.subTest(channel_ref=channel_ref), self.assertRaises(ValueError):
                    resolve_channel_id(channel_ref, "yt-dlp")
        html.assert_not_called()
        run.assert_not_called()

    def test_resolve_channel_id_rejects_malformed_handle_before_request(self):
        with (
            mock.patch("ytb_tg_backup.youtube._resolve_channel_id_from_html") as html,
            mock.patch("ytb_tg_backup.youtube.subprocess.run") as run,
        ):
            for channel_ref in ("@", "@a/b", "@name?next=evil", "@name#fragment"):
                with self.subTest(channel_ref=channel_ref), self.assertRaises(ValueError):
                    resolve_channel_id(channel_ref, "yt-dlp")
        html.assert_not_called()
        run.assert_not_called()

    def test_resolve_channel_id_rejects_plain_handle_without_at(self):
        with self.assertRaises(ValueError):
            resolve_channel_id("rnqqU", "yt-dlp")


if __name__ == "__main__":
    unittest.main()
