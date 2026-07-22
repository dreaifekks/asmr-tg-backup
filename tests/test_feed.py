import unittest
from unittest import mock

from ytb_tg_backup.feed import MAX_FEED_BYTES, _quote_url_for_request, extract_video_id, fetch_feed, parse_feed


class _LargeResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, size: int = -1):
        return b"x" * (MAX_FEED_BYTES + 1)


class FeedParsingTest(unittest.TestCase):
    def test_parse_youtube_atom_feed(self):
        xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>yt:video:abc123XYZ_-</id>
    <yt:videoId>abc123XYZ_-</yt:videoId>
    <title>Demo</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=abc123XYZ_-"/>
    <published>2026-06-07T00:00:00+00:00</published>
  </entry>
</feed>
"""
        entries = parse_feed(xml, "feed", "Feed")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].video_id, "abc123XYZ_-")
        self.assertEqual(entries[0].title, "Demo")

    def test_parse_rsshub_style_rss_feed(self):
        xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>RSS Demo</title>
      <link>https://www.youtube.com/watch?v=def456XYZ_-</link>
      <pubDate>Sun, 07 Jun 2026 00:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""
        entries = parse_feed(xml, "feed", "Feed")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].video_id, "def456XYZ_-")
        self.assertEqual(entries[0].published_at, "2026-06-07T00:00:00+00:00")

    def test_extract_video_id_variants(self):
        self.assertEqual(extract_video_id("https://youtu.be/abc123XYZ_-"), "abc123XYZ_-")
        self.assertEqual(extract_video_id("https://www.youtube.com/shorts/abc123XYZ_-"), "abc123XYZ_-")
        self.assertEqual(extract_video_id("yt:video:abc123XYZ_-"), "abc123XYZ_-")

    def test_quote_url_for_youtube_feed_query(self):
        url = _quote_url_for_request("https://www.youtube.com/feeds/videos.xml?channel_id=UC123")
        self.assertEqual(url, "https://www.youtube.com/feeds/videos.xml?channel_id=UC123")

    def test_fetch_feed_rejects_oversized_response(self):
        with mock.patch("ytb_tg_backup.feed.urlopen", return_value=_LargeResponse()):
            with self.assertRaisesRegex(ValueError, "feed response exceeds"):
                fetch_feed("https://example.invalid/feed.xml")


if __name__ == "__main__":
    unittest.main()
