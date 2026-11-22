"""视频媒体 API（#192）：同源本机服务、登记版本与 Range 边界。"""

from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill" / "annotation-everything" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT))

import host_isolation  # noqa: E402

host_isolation.require_for_module(__name__)
os.environ["AE_HOME"] = tempfile.mkdtemp(prefix="ae-video-server-home-")

import build_viewer  # noqa: E402
import serve  # noqa: E402
from tests.test_video_review import synthetic_mp4  # noqa: E402


class VideoServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-video-server-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "synthetic.mp4"
        self.media = synthetic_mp4()
        self.source.write_bytes(self.media)
        self.review = self.root / "review"
        build_viewer.build(self.source, None, self.review, no_scan_skills=True, locale="zh-CN")
        self.server = serve.ReviewServer(self.review, 0, log_to_file=False)
        self.worker = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.worker.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)

    def request(self, method, path, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=3)
        self.addCleanup(connection.close)
        request_headers = {"Host": f"127.0.0.1:{self.server.port}", **(headers or {})}
        connection.request(method, path, headers=request_headers)
        response = connection.getresponse()
        body = response.read()
        return response, body

    def test_metadata_and_health_expose_only_registered_video_capability(self):
        response, body = self.request("GET", "/api/video/versions")
        self.assertEqual(response.status, 200)
        data = json.loads(body)
        self.assertEqual(data["reviewId"], json.loads((self.review / "review.json").read_text())["reviewId"])
        self.assertEqual(data["video"]["currentVersionId"], "V1")
        version = data["video"]["versions"][0]
        self.assertNotIn("path", version)
        self.assertEqual(version["url"], "/api/video/versions/V1/media")
        self.assertEqual(version["fps"], 30)
        self.assertNotIn("Access-Control-Allow-Origin", response.headers)

        response, body = self.request("GET", "/api/video/versions/V1")
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(body)["videoVersion"]["versionId"], "V1")
        response, _body = self.request("GET", "/api/video/versions/V2")
        self.assertEqual(response.status, 404)

        response, body = self.request("GET", "/api/health")
        self.assertIn("video", json.loads(body)["features"])

    def test_get_head_and_single_ranges_return_the_private_v1_bytes(self):
        response, body = self.request("GET", "/api/video/versions/V1/media")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Content-Type"], "video/mp4")
        self.assertEqual(response.headers["Accept-Ranges"], "bytes")
        self.assertEqual(body, self.media)

        response, body = self.request("HEAD", "/api/video/versions/V1/media")
        self.assertEqual(response.status, 200)
        self.assertEqual(int(response.headers["Content-Length"]), len(self.media))
        self.assertEqual(body, b"")

        for header, expected, content_range in (
            ("bytes=8-27", self.media[8:28], f"bytes 8-27/{len(self.media)}"),
            ("bytes=9-", self.media[9:], f"bytes 9-{len(self.media) - 1}/{len(self.media)}"),
            ("bytes=-11", self.media[-11:], f"bytes {len(self.media) - 11}-{len(self.media) - 1}/{len(self.media)}"),
        ):
            with self.subTest(header=header):
                response, body = self.request("GET", "/api/video/versions/V1/media", {"Range": header})
                self.assertEqual(response.status, 206)
                self.assertEqual(response.headers["Content-Range"], content_range)
                self.assertEqual(body, expected)

    def test_invalid_ranges_and_unregistered_or_arbitrary_sources_are_rejected(self):
        for header in ("bytes=999999-", "bytes=20-10", "bytes=1-2,4-5", "items=1-2"):
            with self.subTest(header=header):
                response, _body = self.request("GET", "/api/video/versions/V1/media", {"Range": header})
                self.assertEqual(response.status, 416)
                self.assertEqual(response.headers["Content-Range"], f"bytes */{len(self.media)}")
        for path in (
            "/api/video/versions/V2/media",
            "/api/video/media?path=/etc/passwd",
            "/media/v1.mp4",
            "/api/video/versions/V1/media/extra",
        ):
            with self.subTest(path=path):
                response, _body = self.request("GET", path)
                self.assertEqual(response.status, 404)

    def test_manifest_cannot_escape_review_or_follow_a_media_symlink(self):
        outside = self.root / "other-review.mp4"
        outside.write_bytes(b"other review must not be readable")
        link = self.review / "media" / "linked.mp4"
        link.symlink_to(outside)
        manifest_path = self.review / "review.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        original = manifest["video"]["versions"][0]["path"]
        for path in ("media/linked.mp4", "../outside.mp4", "/etc/passwd"):
            manifest["video"]["versions"][0]["path"] = path
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            self.server._cache.clear()
            with self.subTest(path=path):
                response, _body = self.request("GET", "/api/video/versions/V1/media")
                self.assertEqual(response.status, 404)
        manifest["video"]["versions"][0]["path"] = original
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def test_index_csp_allows_only_same_origin_api_media_in_addition_to_embedded_data(self):
        response, _body = self.request("GET", "/index.html")
        csp = response.headers["Content-Security-Policy"]
        self.assertIn("media-src 'self' data: blob:", csp)
        self.assertNotIn("media-src *", csp)


if __name__ == "__main__":
    unittest.main()
