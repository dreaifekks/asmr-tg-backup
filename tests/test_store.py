from pathlib import Path
import tempfile
import unittest

from ytb_tg_backup.feed import FeedEntry
from ytb_tg_backup.store import Store


class StoreTest(unittest.TestCase):
    def test_store_upsert_and_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store = Store(tmp_path / "state.db")
            store.initialize()
            entry = FeedEntry(
                feed_id="feed",
                feed_name="Feed",
                video_id="abc123",
                title="Demo",
                url="https://www.youtube.com/watch?v=abc123",
                published_at=None,
                updated_at=None,
            )

            self.assertTrue(store.upsert_entry(entry))
            self.assertFalse(store.upsert_entry(entry))
            self.assertEqual(store.counts_by_status(), {"seen": 1})

            rows = store.list_pending(limit=10, max_attempts=5, include_downloaded=False)
            self.assertEqual(len(rows), 1)
            store.mark_downloaded("abc123", tmp_path / "abc123.mp4", 10)
            self.assertEqual(store.counts_by_status(), {"downloaded": 1})
            self.assertEqual(store.backup_summary()["backed_up"], 1)

    def test_subscriptions_and_bot_offset(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "state.db")
            store.initialize()
            created = store.upsert_subscription(
                sub_id="asmr",
                name="ASMR",
                channel_id="@asmr",
                routes=["live"],
                created_by="123",
            )
            self.assertTrue(created)
            self.assertEqual(len(store.list_subscriptions()), 1)
            self.assertEqual(store.list_subscriptions()[0].routes, ["live"])

            updated = store.upsert_subscription(
                sub_id="asmr",
                name="ASMR Updated",
                channel_id="@asmr2",
                routes=["channel", "live"],
                created_by="123",
            )
            self.assertFalse(updated)
            self.assertEqual(store.list_subscriptions()[0].channel_id, "@asmr2")

            self.assertEqual(store.get_bot_offset(), 0)
            store.set_bot_offset(42)
            self.assertEqual(store.get_bot_offset(), 42)
            self.assertTrue(store.delete_subscription("asmr"))


if __name__ == "__main__":
    unittest.main()
