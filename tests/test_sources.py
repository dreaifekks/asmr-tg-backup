import json
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
import unittest
from unittest import mock

from ytb_tg_backup.config import TwitchConfig
from ytb_tg_backup.models import Origin
from ytb_tg_backup.sources import (
    RssSource,
    SourceError,
    SourceRegistry,
    TwitchHelixSource,
    YouTubePublicSource,
)


class _JsonResponse:
    def __init__(self, payload):
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, size: int = -1):
        return self.body if size < 0 else self.body[:size]


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
        source = TwitchHelixSource(
            TwitchConfig(
                client_id="client-id",
                access_token="expired-token",
                client_secret="client-secret",
            )
        )
        unauthorized = HTTPError(
            "https://api.twitch.tv/helix/videos",
            401,
            "Unauthorized",
            {},
            None,
        )
        self.addCleanup(unauthorized.close)
        success = {
            "data": [_video("200", published_at="2026-07-22T12:00:00Z")],
            "pagination": {},
        }

        with mock.patch(
            "ytb_tg_backup.sources.urlopen",
            side_effect=(
                unauthorized,
                _JsonResponse({"access_token": "fresh-token"}),
                _JsonResponse(success),
            ),
        ) as urlopen:
            result = source.discover(_twitch_origin(bootstrap="latest"))

        self.assertEqual([item.external_id for item in result.items], ["200"])
        self.assertEqual(source._access_token, "fresh-token")
        self.assertEqual(urlopen.call_count, 3)

        first_request = urlopen.call_args_list[0].args[0]
        oauth_request = urlopen.call_args_list[1].args[0]
        retry_request = urlopen.call_args_list[2].args[0]
        self.assertEqual(first_request.get_header("Authorization"), "Bearer expired-token")
        self.assertEqual(retry_request.get_header("Authorization"), "Bearer fresh-token")
        self.assertEqual(urlsplit(oauth_request.full_url).path, "/oauth2/token")
        self.assertEqual(
            parse_qs(oauth_request.data.decode("ascii")),
            {
                "client_id": ["client-id"],
                "client_secret": ["client-secret"],
                "grant_type": ["client_credentials"],
            },
        )

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
        registry = SourceRegistry(TwitchConfig())

        youtube = registry.get("youtube")
        twitch = registry.get("twitch")
        self.assertIsInstance(youtube, YouTubePublicSource)
        self.assertEqual(youtube.provider, "youtube")
        self.assertIsInstance(twitch, TwitchHelixSource)
        self.assertEqual(twitch.provider, "twitch")

    def test_registry_rejects_unknown_provider(self):
        registry = SourceRegistry(TwitchConfig())

        with self.assertRaises(SourceError) as caught:
            registry.get("unknown")
        self.assertEqual(caught.exception.code, "invalid_origin")

        with self.assertRaises(SourceError) as caught:
            registry.get("youtube", "members")
        self.assertEqual(caught.exception.code, "invalid_origin")


if __name__ == "__main__":
    unittest.main()
