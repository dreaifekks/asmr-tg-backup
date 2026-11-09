from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import subprocess
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
import unittest
from unittest import mock

from ytb_tg_backup.config import TwitchConfig
from ytb_tg_backup.extension_api import HttpResponse
from ytb_tg_backup.extensions import RuntimeBuilder
from ytb_tg_backup.models import MediaCandidate, Origin
from ytb_tg_backup.network import ConnectionRuntime
from ytb_tg_backup.sources import (
    RssSource,
    SourceError,
    register_builtin_source_providers,
    TwitchHelixSource,
    YouTubePublicSource,
    _next_tokyo_noon,
)
from ytb_tg_backup.youtube import list_recent_channel_videos


def _twitch_origin(*, bootstrap: str = "all") -> Origin:
    return Origin(
        id="twitch-example",
        provider="twitch",
        kind="vods",
        name="Twitch Example",
        external_id="12345",
        bootstrap=bootstrap,
    )


def _video(video_id: str, *, published_at: str) -> dict[str, object]:
    return {
        "id": video_id,
        "type": "archive",
        "title": f"VOD {video_id}",
        "url": f"https://www.twitch.tv/videos/{video_id}",
        "published_at": published_at,
        "viewable": "public",
        "duration": "1h2m3s",
    }


class TwitchHelixSourceTest(unittest.TestCase):
    def test_login_lookup_is_persisted_until_next_tokyo_noon(self):
        source = TwitchHelixSource(
            TwitchConfig(
                client_id="client",
                access_token="token",
                recording_mode="live",
            )
        )
        origin = Origin(
            id="twitch-login-live",
            provider="twitch",
            kind="vods",
            name="Twitch Login Live",
            external_id="example_login",
            options={"recording_mode": "live"},
        )
        responses = (
            {"data": [{"id": "12345"}]},
            {"data": [], "pagination": {}},
            {"data": [], "pagination": {}},
        )

        with mock.patch.object(source, "_api_json", side_effect=responses) as api_json:
            first = source.discover(origin)
            second = source.discover(origin, first.cursor)

        first_checkpoint = json.loads(first.cursor)
        refresh_after = datetime.fromisoformat(
            first_checkpoint["broadcaster_refresh_after"]
        )
        self.assertEqual(first_checkpoint["broadcaster_lookup"], "example_login")
        self.assertEqual(first_checkpoint["broadcaster_id"], "12345")
        self.assertEqual(first_checkpoint["version"], 2)
        self.assertEqual(
            (refresh_after.astimezone(timezone.utc).hour, refresh_after.minute),
            (3, 0),
        )
        self.assertEqual(second.cursor, first.cursor)
        self.assertEqual(
            [call.args[0] for call in api_json.call_args_list],
            ["users", "streams", "streams"],
        )

    def test_expired_login_lookup_refreshes_before_stream_poll(self):
        source = TwitchHelixSource(
            TwitchConfig(
                client_id="client",
                access_token="token",
                recording_mode="live",
            )
        )
        origin = Origin(
            id="twitch-login-live",
            provider="twitch",
            kind="vods",
            name="Twitch Login Live",
            external_id="example_login",
            options={"recording_mode": "live"},
        )
        checkpoint = json.dumps(
            {
                "broadcaster_lookup": "example_login",
                "broadcaster_id": "11111",
                "broadcaster_refresh_after": "2000-01-01T03:00:00+00:00",
                "version": 2,
            }
        )

        with mock.patch.object(
            source,
            "_api_json",
            side_effect=(
                {"data": [{"id": "22222"}]},
                {"data": [], "pagination": {}},
            ),
        ) as api_json:
            result = source.discover(origin, checkpoint)

        refreshed = json.loads(result.cursor)
        self.assertEqual(refreshed["broadcaster_id"], "22222")
        self.assertEqual(
            [call.args[0] for call in api_json.call_args_list],
            ["users", "streams"],
        )

    def test_next_tokyo_noon_is_same_day_before_noon_and_next_day_at_noon(self):
        self.assertEqual(
            _next_tokyo_noon(datetime(2026, 7, 25, 2, 59, tzinfo=timezone.utc)),
            datetime(2026, 7, 25, 3, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            _next_tokyo_noon(datetime(2026, 7, 25, 3, 0, tzinfo=timezone.utc)),
            datetime(2026, 7, 26, 3, 0, tzinfo=timezone.utc),
        )

    def test_login_lookup_rejects_non_object_user_items(self):
        source = TwitchHelixSource(TwitchConfig(client_id="client", access_token="token"))
        origin = Origin(
            id="twitch-login",
            provider="twitch",
            kind="vods",
            name="Twitch Login",
            external_id="example_login",
        )

        with mock.patch.object(source, "_api_json", return_value={"data": ["invalid"]}):
            with self.assertRaises(SourceError) as caught:
                source.discover(origin)

        self.assertEqual(caught.exception.code, "invalid_response")

    def test_two_page_discovery_stops_at_watermark_and_returns_new_checkpoint(self):
        source = TwitchHelixSource(
            TwitchConfig(client_id="client", access_token="token", max_pages_per_poll=2)
        )
        checkpoint = json.dumps(
            {
                "published_at": "2026-07-21T12:00:00Z",
                "external_id": "100",
            }
        )
        pages = (
            {
                "data": [
                    _video("300", published_at="2026-07-22T12:00:00Z"),
                    _video("250", published_at="2026-07-22T11:00:00Z"),
                ],
                "pagination": {"cursor": "page-two"},
            },
            {
                "data": [
                    _video("200", published_at="2026-07-21T13:00:00Z"),
                    _video("100", published_at="2026-07-21T12:00:00Z"),
                    _video("050", published_at="2026-07-21T11:00:00Z"),
                ],
                "pagination": {"cursor": "page-three"},
            },
        )

        with mock.patch.object(source, "_api_json", side_effect=pages) as api_json:
            result = source.discover(_twitch_origin(), checkpoint)

        self.assertEqual([item.external_id for item in result.items], ["300", "250", "200"])
        self.assertEqual(
            json.loads(result.cursor),
            {
                "published_at": "2026-07-22T12:00:00Z",
                "external_id": "300",
                "version": 1,
            },
        )
        self.assertEqual(api_json.call_count, 2)
        self.assertEqual(
            api_json.call_args_list[0].args,
            ("videos", {"user_id": "12345", "type": "archive", "first": "100"}),
        )
        self.assertEqual(
            api_json.call_args_list[1].args,
            (
                "videos",
                {
                    "user_id": "12345",
                    "type": "archive",
                    "first": "100",
                    "after": "page-two",
                },
            ),
        )

    def test_second_page_failure_does_not_return_a_success_checkpoint(self):
        source = TwitchHelixSource(
            TwitchConfig(client_id="client", access_token="token", max_pages_per_poll=2)
        )
        checkpoint = json.dumps(
            {
                "published_at": "2026-07-21T12:00:00Z",
                "external_id": "100",
            }
        )
        first_page = {
            "data": [_video("200", published_at="2026-07-22T12:00:00Z")],
            "pagination": {"cursor": "page-two"},
        }

        with mock.patch.object(
            source,
            "_api_json",
            side_effect=(first_page, SourceError("page two failed", code="network_error")),
        ) as api_json:
            with self.assertRaisesRegex(SourceError, "page two failed") as caught:
                source.discover(_twitch_origin(), checkpoint)

        self.assertEqual(caught.exception.code, "network_error")
        self.assertEqual(api_json.call_count, 2)

    def test_401_refreshes_app_token_and_retries_once(self):
        http = mock.Mock()
        source = TwitchHelixSource(
            TwitchConfig(client_id="client-id", access_token="expired-token", client_secret="client-secret"),
            http,
        )
        success = {
            "data": [_video("200", published_at="2026-07-22T12:00:00Z")],
            "pagination": {},
        }
        http.request.side_effect = [
            HttpResponse(401, "", {}, b"{}"),
            HttpResponse(200, "", {}, b'{"access_token":"fresh-token"}'),
            HttpResponse(200, "", {}, json.dumps(success).encode()),
        ]
        result = source.discover(_twitch_origin(bootstrap="latest"))
        self.assertEqual([item.external_id for item in result.items], ["200"])
        self.assertEqual(source._access_token, "fresh-token")
        self.assertEqual(http.request.call_count, 3)
        first, oauth, retry = [call.args[0] for call in http.request.call_args_list]
        self.assertEqual(first.headers["Authorization"], "Bearer expired-token")
        self.assertEqual(retry.headers["Authorization"], "Bearer fresh-token")
        self.assertEqual(urlsplit(oauth.url).path, "/oauth2/token")
        self.assertEqual(parse_qs(oauth.body.decode()), {
            "client_id": ["client-id"], "client_secret": ["client-secret"],
            "grant_type": ["client_credentials"],
        })

    def test_deleted_checkpoint_uses_published_watermark_as_safe_boundary(self):
        source = TwitchHelixSource(
            TwitchConfig(client_id="client", access_token="token", max_pages_per_poll=2)
        )
        checkpoint = json.dumps(
            {"published_at": "2026-07-21T12:00:00Z", "external_id": "deleted"}
        )
        page = {
            "data": [
                _video("new", published_at="2026-07-22T12:00:00Z"),
                _video("old", published_at="2026-07-20T12:00:00Z"),
            ],
            "pagination": {"cursor": "unused"},
        }

        with mock.patch.object(source, "_api_json", return_value=page) as api_json:
            result = source.discover(_twitch_origin(), checkpoint)

        self.assertEqual([item.external_id for item in result.items], ["new"])
        self.assertEqual(json.loads(result.cursor)["external_id"], "new")
        api_json.assert_called_once()


YOUTUBE_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <yt:videoId>feed1</yt:videoId>
    <title>Feed video</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=feed1"/>
    <published>2026-11-09T01:00:00+00:00</published>
  </entry>
</feed>
"""


def _youtube_origin(kind: str = "uploads") -> Origin:
    return Origin(
        id="yt-example",
        provider="youtube",
        kind=kind,
        name="YouTube Example",
        external_id="UCabcdefghij1234567890",
    )


def _fallback_candidate(video_id: str) -> MediaCandidate:
    return MediaCandidate(
        provider="youtube",
        content_kind="video",
        external_id=video_id,
        title=f"ASMR {video_id}",
        url=f"https://www.youtube.com/watch?v={video_id}",
        published_at=None,
    )


def _failing_fetcher(exc: Exception):
    def fetch(_url: str) -> bytes:
        raise exc

    return fetch


def _feed_checkpoint(feed_success_at: datetime) -> str:
    return f'{{"feed_success_at":"{feed_success_at.isoformat(timespec="seconds")}","version":1}}'


def _feed_success_hours_ago(hours: float) -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=hours)


class YouTubePublicSourceTest(unittest.TestCase):
    logger = logging.getLogger("test.youtube_fallback")

    def test_feed_404_5xx_and_network_errors_use_yt_dlp_fallback(self):
        failures = (
            (SourceError("youtube source HTTP 404", code="http_404"), "http_404"),
            (SourceError("youtube source HTTP 500", code="http_500"), "http_500"),
            (SourceError("youtube source HTTP 503", code="http_503"), "http_503"),
            (URLError("connection reset"), "network_error"),
            (TimeoutError("timed out"), "network_error"),
        )
        feed_success_at = _feed_success_hours_ago(3)
        checkpoint = _feed_checkpoint(feed_success_at)
        for kind in ("uploads", "vod_after_live"):
            for feed_error, code in failures:
                with self.subTest(kind=kind, code=code):
                    fallback = mock.Mock(return_value=[_fallback_candidate("fresh1")])
                    source = YouTubePublicSource(
                        _failing_fetcher(feed_error),
                        fallback=fallback,
                        logger=self.logger,
                    )

                    with self.assertLogs(self.logger, logging.INFO) as logs:
                        result = source.discover(_youtube_origin(kind), checkpoint)

                    # Videos up to two hours older than the last successful feed
                    # poll may still have been missing from that feed.
                    fallback.assert_called_once_with(
                        "UCabcdefghij1234567890",
                        feed_success_at - timedelta(hours=2),
                    )
                    self.assertEqual([item.external_id for item in result.items], ["fresh1"])
                    # Only a successful feed poll moves the boundary.
                    self.assertEqual(result.cursor, checkpoint)
                    self.assertEqual(
                        logs.output,
                        [
                            "INFO:test.youtube_fallback:youtube rss failed origin=yt-example "
                            f"code={code}; fallback=yt-dlp items=1"
                        ],
                    )

    def test_fallback_lists_at_most_10_hours_back(self):
        # Beyond 12 hours the listing would see "N days ago" times rounded to
        # a UTC midnight, which cannot place a video relative to the feed.
        fallback = mock.Mock(return_value=[])
        source = YouTubePublicSource(
            _failing_fetcher(SourceError("youtube source HTTP 404", code="http_404")),
            fallback=fallback,
            logger=self.logger,
        )
        checkpoint = _feed_checkpoint(_feed_success_hours_ago(30))

        before = datetime.now(timezone.utc)
        with self.assertLogs(self.logger, logging.INFO):
            result = source.discover(_youtube_origin(), checkpoint)
        after = datetime.now(timezone.utc)

        since = fallback.call_args.args[1]
        self.assertLessEqual(before - timedelta(hours=10), since)
        self.assertLessEqual(since, after - timedelta(hours=10))
        self.assertEqual(result.cursor, checkpoint)

    def test_feed_failure_without_checkpoint_raises_without_fallback(self):
        # New sources and sources that have not had a successful feed poll
        # since upgrading leave discovery, including the initial seed, to the feed.
        for feed_error, code in (
            (SourceError("youtube source HTTP 404", code="http_404"), "http_404"),
            (URLError("connection reset"), "network_error"),
        ):
            with self.subTest(code=code):
                fallback = mock.Mock()
                source = YouTubePublicSource(
                    _failing_fetcher(feed_error),
                    fallback=fallback,
                    logger=self.logger,
                )

                with (
                    self.assertLogs(self.logger, logging.INFO) as logs,
                    self.assertRaises(type(feed_error)) as caught,
                ):
                    source.discover(_youtube_origin(), None)

                self.assertIs(caught.exception, feed_error)
                fallback.assert_not_called()
                self.assertEqual(
                    logs.output,
                    [
                        "INFO:test.youtube_fallback:youtube rss failed origin=yt-example "
                        f"code={code}; fallback=skipped: no successful rss poll yet"
                    ],
                )

    def test_successful_feed_records_feed_success_time(self):
        fallback = mock.Mock()
        source = YouTubePublicSource(lambda _url: YOUTUBE_FEED, fallback=fallback)
        previous = _feed_checkpoint(_feed_success_hours_ago(3))

        before = datetime.now(timezone.utc).replace(microsecond=0)
        result = source.discover(_youtube_origin(), previous)
        after = datetime.now(timezone.utc)

        fallback.assert_not_called()
        self.assertEqual([item.external_id for item in result.items], ["feed1"])
        self.assertEqual(result.items[0].published_at, "2026-11-09T01:00:00+00:00")
        feed_success_at = datetime.fromisoformat(json.loads(result.cursor)["feed_success_at"])
        self.assertEqual(result.cursor, _feed_checkpoint(feed_success_at))
        self.assertLessEqual(before, feed_success_at)
        self.assertLessEqual(feed_success_at, after)

    def test_empty_feed_neither_creates_nor_moves_checkpoint(self):
        # An empty feed shows no video it covered: a source that has only seen
        # empty feeds keeps waiting for the feed, and an existing boundary stays.
        empty_feed = (
            b'<?xml version="1.0" encoding="UTF-8"?>\n'
            b'<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns="http://www.w3.org/2005/Atom">\n</feed>\n'
        )
        source = YouTubePublicSource(lambda _url: empty_feed, fallback=mock.Mock())
        for checkpoint in (None, _feed_checkpoint(_feed_success_hours_ago(3))):
            with self.subTest(checkpoint=checkpoint):
                result = source.discover(_youtube_origin(), checkpoint)

                self.assertEqual(result.items, [])
                self.assertEqual(result.cursor, checkpoint)

    def test_other_feed_errors_are_raised_without_fallback(self):
        bad_request = HTTPError("https://www.youtube.com/feeds/videos.xml", 400, "Bad Request", {}, None)
        self.addCleanup(bad_request.close)
        for feed_error in (
            SourceError("youtube source HTTP 403", code="http_403"),
            SourceError("youtube source HTTP 429", code="http_429"),
            bad_request,
        ):
            with self.subTest(error=str(feed_error)):
                fallback = mock.Mock()
                source = YouTubePublicSource(_failing_fetcher(feed_error), fallback=fallback)

                with self.assertRaises(type(feed_error)) as caught:
                    source.discover(_youtube_origin(), _feed_checkpoint(_feed_success_hours_ago(3)))

                self.assertIs(caught.exception, feed_error)
                fallback.assert_not_called()

    def test_failed_fallback_raises_original_feed_error_and_logs_reason(self):
        feed_error = SourceError("youtube source HTTP 404", code="http_404")
        timeout = subprocess.TimeoutExpired(["yt-dlp"], 30)
        source = YouTubePublicSource(
            _failing_fetcher(feed_error),
            fallback=lambda channel_id, since: list_recent_channel_videos(
                channel_id,
                since,
                ["yt-dlp"],
                ConnectionRuntime(),
            ),
            logger=self.logger,
        )

        with (
            mock.patch("ytb_tg_backup.youtube.subprocess.run", side_effect=timeout) as run,
            self.assertLogs(self.logger, logging.WARNING) as logs,
            self.assertRaises(SourceError) as caught,
        ):
            source.discover(_youtube_origin(), _feed_checkpoint(_feed_success_hours_ago(3)))

        self.assertIs(caught.exception, feed_error)
        self.assertEqual(caught.exception.code, "http_404")
        self.assertIsNone(caught.exception.__cause__)
        self.assertEqual(run.call_args.kwargs["timeout"], 30)
        self.assertEqual(len(logs.output), 1)
        self.assertIn(
            "youtube rss failed origin=yt-example code=http_404; fallback=yt-dlp failed: TimeoutExpired:",
            logs.output[0],
        )
        self.assertIn("timed out after 30 seconds", logs.output[0])

    def test_failed_fallback_after_network_error_keeps_original_exception(self):
        feed_error = URLError("connection refused")
        source = YouTubePublicSource(
            _failing_fetcher(feed_error),
            fallback=mock.Mock(side_effect=RuntimeError("yt-dlp /streams listing exited with 1")),
            logger=self.logger,
        )

        with self.assertLogs(self.logger, logging.WARNING) as logs, self.assertRaises(URLError) as caught:
            source.discover(_youtube_origin(), _feed_checkpoint(_feed_success_hours_ago(3)))

        self.assertIs(caught.exception, feed_error)
        self.assertIn("code=network_error; fallback=yt-dlp failed: RuntimeError:", logs.output[0])


class RssSourceTest(unittest.TestCase):
    @staticmethod
    def _feed(url: str, published: str = "2026-07-22T00:00:00Z") -> bytes:
        return f"""
<rss version="2.0"><channel><item>
  <title>Media</title><link>{url}</link><guid>../unsafe*?[guid]</guid>
  <published>{published}</published>
</item></channel></rss>
""".encode()

    def test_url_hash_identity_is_safe_and_does_not_collide_on_query_ids(self):
        public_address = [(2, 1, 6, "", ("93.184.216.34", 443))]
        with mock.patch("ytb_tg_backup.sources.socket.getaddrinfo", return_value=public_address):
            first = RssSource(lambda _: self._feed("https://a.example/watch?v=same123")).discover(
                Origin("a", "rss", "feed", "A", "https://a.example/feed")
            )
            second = RssSource(lambda _: self._feed("https://b.example/post?v=same123")).discover(
                Origin("b", "rss", "feed", "B", "https://b.example/feed")
            )

        first_id = first.items[0].external_id
        second_id = second.items[0].external_id
        self.assertNotEqual(first_id, second_id)
        self.assertRegex(first_id, r"^url-[0-9a-f]{64}$")
        self.assertEqual(first.items[0].metadata["feed_entry_id"], "same123")

    def test_private_media_url_is_rejected_before_download(self):
        private_address = [(2, 1, 6, "", ("127.0.0.1", 80))]
        source = RssSource(lambda _: self._feed("http://127.0.0.1/admin"))
        with mock.patch("ytb_tg_backup.sources.socket.getaddrinfo", return_value=private_address):
            with self.assertRaises(SourceError) as caught:
                source.discover(Origin("rss", "rss", "feed", "RSS", "https://feed.example/rss"))
        self.assertEqual(caught.exception.code, "unsafe_media_url")


class SourceRegistryTest(unittest.TestCase):
    def test_registry_contains_youtube_and_twitch_adapters(self):
        builder = RuntimeBuilder()
        register_builtin_source_providers(builder.scoped("core"), SimpleNamespace(twitch=TwitchConfig()))
        registry = builder.build(
            host=mock.Mock(), logger=logging.getLogger("test"), data_dir=Path("."),
        ).create_source_registry()

        youtube = registry.get("youtube")
        twitch = registry.get("twitch")
        self.assertIsInstance(youtube, YouTubePublicSource)
        self.assertEqual(youtube.provider, "youtube")
        self.assertIsInstance(twitch, TwitchHelixSource)
        self.assertEqual(twitch.provider, "twitch")

    def test_registry_rejects_unknown_provider(self):
        builder = RuntimeBuilder()
        register_builtin_source_providers(builder.scoped("core"), SimpleNamespace(twitch=TwitchConfig()))
        registry = builder.build(
            host=mock.Mock(), logger=logging.getLogger("test"), data_dir=Path("."),
        ).create_source_registry()

        with self.assertRaises(SourceError) as caught:
            registry.get("unknown")
        self.assertEqual(caught.exception.code, "invalid_origin")

        with self.assertRaises(SourceError) as caught:
            registry.get("youtube", "members")
        self.assertEqual(caught.exception.code, "invalid_origin")


if __name__ == "__main__":
    unittest.main()
