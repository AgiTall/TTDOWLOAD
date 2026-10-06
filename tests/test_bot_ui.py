import os
import io
import unittest
from unittest.mock import patch

import bot
from media_downloader import (MediaDownloadError, is_spotify_track_url,
                              rank_music_results, search_music)


class BotUiTests(unittest.TestCase):
    def test_music_results_paginate_five_per_page(self) -> None:
        user_id = 987654321
        bot.search_sources[user_id] = "sc"
        bot.search_results[user_id] = [
            {"title": f"Track {i}", "artist": "Artist", "url": f"https://soundcloud.com/example/{i}"}
            for i in range(12)
        ]
        first, first_markup = bot.result_page(user_id, 0)
        last, last_markup = bot.result_page(user_id, 2)
        self.assertIn("страница 1/3", first)
        self.assertIn("Track 4", first)
        self.assertNotIn("Track 5", first)
        self.assertIn("страница 3/3", last)
        self.assertIn("Track 11", last)
        self.assertNotIn("Track 9", last)
        self.assertTrue(any(button.callback_data == "page:1" for row in first_markup.inline_keyboard for button in row))
        self.assertFalse(any(button.callback_data == "page:3" for row in last_markup.inline_keyboard for button in row))

    def test_public_spotify_search_requires_approved_access(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(MediaDownloadError):
                search_music("example track", "spotify")

    def test_youtube_music_ranking_rejects_tv_episodes(self) -> None:
        results = [
            {"title": "Конфетка | 2 сезон | 1 выпуск", "artist": "ТНТ"},
            {"title": "Конфетка - official audio", "artist": "Певец"},
        ]
        ranked = rank_music_results("Конфетка", results)
        self.assertEqual([item["title"] for item in ranked], ["Конфетка - official audio"])

    def test_spotify_track_link_works_without_developer_keys(self) -> None:
        response = io.BytesIO(b'{"title":"Artist - Song"}')
        with patch.dict(os.environ, {}, clear=True), patch("media_downloader.urlopen", return_value=response):
            results = search_music("https://open.spotify.com/track/123", "spotify")
        self.assertEqual(results[0]["source"], "Spotify-ссылка")
        self.assertEqual(results[0]["url"], "https://open.spotify.com/track/123")
        self.assertFalse(results[0]["downloadable"])

    def test_spotify_link_must_be_from_spotify(self) -> None:
        self.assertTrue(is_spotify_track_url("https://open.spotify.com/track/123"))
        self.assertTrue(is_spotify_track_url("https://open.spotify.com/intl-ru/track/123"))
        self.assertFalse(is_spotify_track_url("https://evil.example/?x=open.spotify.com/track/123"))
        self.assertFalse(is_spotify_track_url("https://open.spotify.com/album/123"))

    def test_get_spotify_track_info_opengraph(self) -> None:
        from media_downloader import get_spotify_track_info
        fake_html = (
            b'<html><head>'
            b'<meta property="og:title" content="Test Song" />'
            b'<meta name="music:musician_description" content="Test Artist" />'
            b'<meta property="og:image" content="https://example.com/cover.jpg" />'
            b'</head></html>'
        )
        with patch("media_downloader.urlopen", return_value=io.BytesIO(fake_html)):
            info = get_spotify_track_info("https://open.spotify.com/track/123")
        self.assertEqual(info["title"], "Test Song")
        self.assertEqual(info["artist"], "Test Artist")
        self.assertEqual(info["image"], "https://example.com/cover.jpg")


if __name__ == "__main__":
    unittest.main()
