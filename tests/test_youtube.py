import subprocess
import unittest
from unittest import mock

from ytb_tg_backup.youtube import resolve_channel_id, youtube_channel_feed_url


class _HtmlResponse:
    def __init__(self, html: str):
        self.body = html.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self.body


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

    def test_resolve_channel_id_uses_injected_opener_for_html_lookup(self):
        channel_id = "UCabcdefghij1234567890"
        opener = mock.Mock(
            return_value=_HtmlResponse(f'{{"channelId":"{channel_id}"}}')
        )

        with mock.patch("ytb_tg_backup.youtube.subprocess.run") as run:
            resolved = resolve_channel_id(
                "@rnqqU",
                "/opt/yt-dlp",
                opener=opener,
                subprocess_env={"http_proxy": "http://127.0.0.1:7890"},
            )

        self.assertEqual(resolved, channel_id)
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, "https://www.youtube.com/@rnqqU")
        self.assertEqual(opener.call_args.kwargs["timeout"], 30)
        run.assert_not_called()

    def test_resolve_channel_id_passes_proxy_environment_to_yt_dlp_fallback(self):
        completed = subprocess.CompletedProcess(
            ["yt-dlp"],
            0,
            stdout="UCabcdefghij1234567890\n",
            stderr="",
        )
        child_env = {
            "http_proxy": "http://127.0.0.1:7890",
            "https_proxy": "http://127.0.0.1:7890",
        }
        with (
            mock.patch(
                "ytb_tg_backup.youtube._resolve_channel_id_from_html",
                return_value=None,
            ),
            mock.patch(
                "ytb_tg_backup.youtube.subprocess.run",
                return_value=completed,
            ) as run,
        ):
            resolved = resolve_channel_id(
                "@rnqqU",
                "/opt/yt-dlp",
                subprocess_env=child_env,
            )

        self.assertEqual(resolved, "UCabcdefghij1234567890")
        self.assertEqual(run.call_args.kwargs["env"], child_env)

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
