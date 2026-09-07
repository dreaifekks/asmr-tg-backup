from dataclasses import replace
import json
import logging
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from ytb_tg_backup.config import load_config, TwitchConfig
from ytb_tg_backup.control import ControlBot
from ytb_tg_backup.extension_api import SourceProviderDefinition
from ytb_tg_backup.extensions import SourceProviderCatalog
from ytb_tg_backup.models import MediaCandidate, Origin
from ytb_tg_backup.single_video import twitch_video_id, youtube_video
from ytb_tg_backup.sources import TwitchHelixSource
from ytb_tg_backup.store import Store


VIDEO_ID = "abcdefghijk"
URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"


class SingleVideoTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        config_path = self.root / "config.toml"
        config_path.write_text(
            f'[app]\ndata_dir = "{self.root}"\n'
            '[control]\nenabled = true\nallowed_user_ids = ["123"]\n'
        )
        self.config = load_config(config_path)
        self.store = Store(self.config.db_path)
        self.store.initialize()
        self.addCleanup(self.store.close)
        self.bot = ControlBot(self.config, self.store, logging.getLogger("test"))
        self.message = {"from": {"id": 123}, "chat": {"id": 123}}

    def test_youtube_url_variants_canonicalize_to_one_video(self):
        for url in (
            URL, f"{URL}&list=PL123&index=2", f"https://youtu.be/{VIDEO_ID}?si=share",
            f"https://m.youtube.com/shorts/{VIDEO_ID}",
            f"https://www.youtube.com/live/{VIDEO_ID}?t=12",
            f"https://www.youtube.com/embed/{VIDEO_ID}",
            f"https://music.youtube.com/watch?v={VIDEO_ID}",
        ):
            with self.subTest(url=url):
                candidate = youtube_video(url)
                self.assertEqual(candidate.external_id, VIDEO_ID)
                self.assertEqual(candidate.url, URL)

    def test_rejects_collections_cross_provider_and_unsafe_urls(self):
        for url in (
            "https://www.youtube.com/playlist?list=PL123", "https://youtu.be/",
            f"https://youtube.com.evil.test/watch?v={VIDEO_ID}",
            f"https://user:pass@youtube.com/watch?v={VIDEO_ID}",
            f"https://youtube.com:443/watch?v={VIDEO_ID}",
            f"http://youtube.com/watch?v={VIDEO_ID}",
            f"{URL}&v=anotherid12", "https://www.twitch.tv/videos/123",
            "https://youtu.be/short", "https://youtu.be/abcdefghijk/extra",
            f"https://you\ntube.com/watch?v={VIDEO_ID}",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                youtube_video(url)
        for url in ("https://www.twitch.tv/channel", "https://clips.twitch.tv/clip", URL):
            with self.subTest(url=url), self.assertRaises(ValueError):
                twitch_video_id(url)

    def test_panel_raw_and_prefixed_input_only_enqueue_one_video(self):
        self.bot._ensure_source_catalog()
        catalog_before = self.config.sources.path.read_bytes()
        for text in (URL, f'url "{URL}"', f'URL "https://youtu.be/{VIDEO_ID}"'):
            state = {"active": True, "awaiting": "add_youtube", "view": "home"}
            with mock.patch.object(self.bot, "_load_panel_state", return_value=state), \
                 mock.patch.object(self.bot, "_panel_state_is_expired", return_value=False), \
                 mock.patch.object(self.bot, "_render_panel_message"), \
                 mock.patch("ytb_tg_backup.control.resolve_channel_id") as resolve:
                self.bot._handle_panel_input(self.message, text)
            resolve.assert_not_called()
            self.assertIsNone(state["awaiting"])
            self.assertIn("单视频备份队列", state["flash"])
        self.assertEqual(self.config.sources.path.read_bytes(), catalog_before)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM media_items").fetchone()[0], 1)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)
        self.assertFalse(any(origin.enabled for origin in self.store.list_origins()))

    def test_channel_input_keeps_subscription_behavior(self):
        for text in ("@example", "https://www.youtube.com/@example"):
            with mock.patch("ytb_tg_backup.control.resolve_channel_id", return_value="UC" + "a" * 22) as resolve:
                self.bot._origin_add(["youtube", text], self.message)
            resolve.assert_called_once()
        self.assertEqual(len(self.store.list_origins(managed_by="catalog")), 1)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)

    def test_invalid_panel_input_stays_pending_without_enqueuing(self):
        for text in ('url', 'url "unterminated', f"url {URL} {URL}",
                     'url "https://www.youtube.com/@example"', f"{URL} {URL}"):
            state = {"active": True, "awaiting": "add_youtube"}
            with mock.patch.object(self.bot, "_load_panel_state", return_value=state), \
                 mock.patch.object(self.bot, "_panel_state_is_expired", return_value=False), \
                 mock.patch.object(self.bot, "_render_panel_message"):
                self.bot._handle_panel_input(self.message, text)
            self.assertEqual(state["awaiting"], "add_youtube")
            self.assertIn("flash_error", state)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)

    def test_unauthorized_or_expired_panel_cannot_enqueue(self):
        self.bot._handle_panel_input({"from": {"id": 999}}, URL)
        with mock.patch.object(self.bot, "_load_panel_state", return_value={"awaiting": "add_youtube"}), \
             mock.patch.object(self.bot, "_panel_state_is_expired", return_value=True), \
             mock.patch.object(self.bot, "_close_panel_state"):
            self.bot._handle_panel_input(self.message, URL)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)

    def test_known_metadata_jobs_and_uncertain_delivery_are_preserved(self):
        candidate = replace(youtube_video(URL), title="Known title", metadata={"key": "value"})
        media_id = self.store.enqueue_single_video(candidate)
        self.store.conn.execute("UPDATE jobs SET state='succeeded' WHERE media_id=?", (media_id,))
        self.store._ensure_job(media_id, "telegram_delivery", "telegram:archive", max_failures=5, state="uncertain")
        self.store.conn.commit()
        self.assertEqual(self.store.enqueue_single_video(youtube_video(URL)), media_id)
        self.assertEqual(self.store.get_media(media_id)["title"], "Known title")
        self.assertEqual(json.loads(self.store.get_media(media_id)["metadata_json"]), {"key": "value"})
        self.assertEqual(
            {row[0] for row in self.store.conn.execute("SELECT state FROM jobs")},
            {"succeeded", "uncertain"},
        )

    def test_manual_request_overrides_initial_seed_and_filter_cancellation(self):
        self.store.upsert_origin(Origin("channel", "youtube", "uploads", "Channel", "UC-example"))
        media_id, _ = self.store.upsert_discovered("channel", youtube_video(URL), disposition="ignored", decision_code="initial_seed")
        self.assertEqual(self.store.enqueue_single_video(youtube_video(URL)), media_id)
        self.store.conn.execute("UPDATE jobs SET state='cancelled', reason_code='source_filter'")
        self.store.conn.commit()
        self.store.enqueue_single_video(youtube_video(URL))
        self.assertEqual(self.store.conn.execute("SELECT state FROM jobs").fetchone()[0], "queued")
        self.store.reconcile_source_catalog([], source_filter="never matches")
        self.assertEqual(len(self.store.media_origins(media_id)), 1)
        self.assertEqual(self.store.media_origins(media_id)[0]["kind"], "manual_url")

    def test_twitch_resolves_only_requested_id_and_uses_actual_kind(self):
        source = TwitchHelixSource(TwitchConfig(client_id="client", access_token="token"))
        for video_type, kind in (("archive", "vod"), ("highlight", "highlight"), ("upload", "upload")):
            with mock.patch.object(source, "_api_json", return_value={"data": [{
                "id": "123", "type": video_type, "title": "Video", "stream_id": "456",
            }]}) as api:
                candidate = source.resolve_video("https://www.twitch.tv/videos/123?t=1h")
            api.assert_called_once_with("videos", {"id": "123"})
            self.assertEqual(candidate.content_kind, kind)
            self.assertEqual(candidate.metadata["stream_id"], "456")
            with mock.patch("ytb_tg_backup.control.TwitchHelixSource.resolve_video", return_value=candidate):
                reply = self.bot._origin_add(["twitch", "vods", "url", candidate.url], self.message, recording_mode="live")
            self.assertIn("单视频", reply)
        self.assertFalse(any(origin.enabled for origin in self.store.list_origins()))

    def test_extension_url_hook_is_optional_and_enqueues_one_candidate(self):
        candidate = MediaCandidate("example", "video", "123", "Video", "https://example.test/watch/123", None)
        definition = SourceProviderDefinition(
            provider="example", kinds=frozenset({"search"}), default_kind="search",
            adapter_factory=lambda _: mock.Mock(), validate_origin=lambda origin: origin,
            identity=lambda origin: (origin.provider, origin.kind, origin.external_id, ""),
            is_live_origin=lambda _: False, seed_content_kind=lambda _: "video", poll_variant=lambda _: None,
        )
        self.bot.source_catalog.providers = SourceProviderCatalog({"example": definition})
        with self.assertRaisesRegex(ValueError, "尚未支持"):
            self.bot._single_video_add("example", ["url", candidate.url])
        resolver = mock.Mock(return_value=candidate)
        self.bot.source_catalog.providers = SourceProviderCatalog({"example": replace(definition, resolve_media_url=resolver)})
        self.bot._origin_add(["example", "url", candidate.url], self.message)
        resolver.assert_called_once_with(candidate.url)
        self.assertEqual(self.store.conn.execute("SELECT provider FROM media_items").fetchone()[0], "example")


if __name__ == "__main__":
    unittest.main()
