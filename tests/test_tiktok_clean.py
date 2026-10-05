import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from media_downloader import (MediaDownloadError, _allowed_tikwm_media,
                              download_media)


class FakeApiResponse(io.BytesIO):
    pass


class TikTokCleanTests(unittest.TestCase):
    def test_media_url_host_must_be_https_and_allowlisted(self):
        self.assertTrue(_allowed_tikwm_media("https://www.tikwm.com/video/media/play/123.mp4"))
        self.assertFalse(_allowed_tikwm_media("http://www.tikwm.com/video.mp4"))
        self.assertFalse(_allowed_tikwm_media("https://tikwm.com.evil.example/video.mp4"))
        self.assertFalse(_allowed_tikwm_media("https://127.0.0.1/video.mp4"))

    def test_watermarked_only_api_response_is_rejected(self):
        payload = {"code": 0, "data": {"id": "123", "wmplay": "/watermarked.mp4"}}
        with tempfile.TemporaryDirectory() as temp:
            with patch("media_downloader.urlopen", return_value=FakeApiResponse(json.dumps(payload).encode())):
                with self.assertRaises(MediaDownloadError):
                    download_media("https://www.tiktok.com/@user/video/123", Path(temp), 25_000_000, "720")


if __name__ == "__main__":
    unittest.main()
