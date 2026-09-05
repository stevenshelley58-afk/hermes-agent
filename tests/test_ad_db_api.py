import os
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from hermes_cli import ad_db_api


class AdDbApiTest(unittest.TestCase):
    def test_media_projection_never_keeps_source_url(self):
        result = ad_db_api._ads([{"id": "ad-1", "media": [{"id": "asset-1", "sourceUrl": "https://cdn.example/a", "sourceURLs": ["https://cdn.example/a"]}]}])
        media = result[0]["media"][0]
        self.assertEqual(media["archiveUrl"], "/v1/ad-db/ads/ad-1/media/asset-1")
        self.assertNotIn("sourceUrl", media)
        self.assertNotIn("sourceURLs", media)

    def test_missing_service_credentials_fail_closed(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(HTTPException) as error:
            ad_db_api._config()
        self.assertEqual(error.exception.status_code, 503)


if __name__ == "__main__":
    unittest.main()
