import unittest

from media_downloader import UnsupportedUrlError, extract_supported_url


class ExtractSupportedUrlTests(unittest.TestCase):
    def test_extracts_tiktok_url_from_text(self) -> None:
        self.assertEqual(
            extract_supported_url("Скачай https://www.tiktok.com/@user/video/123, пожалуйста"),
            "https://www.tiktok.com/@user/video/123",
        )

    def test_accepts_supported_subdomain(self) -> None:
        self.assertEqual(
            extract_supported_url("https://m.youtube.com/watch?v=abc"),
            "https://m.youtube.com/watch?v=abc",
        )

    def test_rejects_domain_suffix_attack(self) -> None:
        with self.assertRaises(UnsupportedUrlError):
            extract_supported_url("https://youtube.com.example.org/video")

    def test_rejects_credentials(self) -> None:
        with self.assertRaises(UnsupportedUrlError):
            extract_supported_url("https://name:password@youtube.com/watch?v=abc")

    def test_rejects_unknown_domain(self) -> None:
        with self.assertRaises(UnsupportedUrlError):
            extract_supported_url("https://example.org/video")

    def test_rejects_invalid_port(self) -> None:
        with self.assertRaises(UnsupportedUrlError):
            extract_supported_url("https://youtube.com:invalid/video")

    def test_requires_url(self) -> None:
        with self.assertRaises(UnsupportedUrlError):
            extract_supported_url("просто текст")


if __name__ == "__main__":
    unittest.main()
