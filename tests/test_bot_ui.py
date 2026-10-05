import os
import unittest
from unittest.mock import patch

import bot
from media_downloader import MediaDownloadError, search_music


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

    def test_spotify_requires_credentials(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(MediaDownloadError):
                search_music("example track", "spotify")


if __name__ == "__main__":
    unittest.main()
