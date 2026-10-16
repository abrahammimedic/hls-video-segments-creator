"""End-to-end upload flow against a fake S3 client + local HTTP server (no real R2 needed).

Builds a tiny real HLS version with ffmpeg, so these tests are skipped when ffmpeg is unavailable.
"""

import argparse
import functools
import http.server
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import make_hls     # noqa: E402
import r2_upload    # noqa: E402

BASE = "exercises/demo/demo_clip"      # bucket folder; v<N> is appended by the uploader
HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


class FakeS3:
    """Just enough of the boto3 S3 client: upload_file + list_objects_v2, backed by a folder."""

    def __init__(self, docroot, fail_after=None):
        self.docroot = docroot
        self.objects = {}          # key -> size
        self.meta = {}             # key -> ExtraArgs
        self.order = []
        self.fail_after = fail_after
        self.lock = threading.Lock()

    def upload_file(self, path, bucket, key, ExtraArgs=None):
        with self.lock:
            if self.fail_after is not None and len(self.order) >= self.fail_after:
                raise RuntimeError("simulated network failure")
            self.order.append(key)
        target = os.path.join(self.docroot, key)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copyfile(path, target)
        with self.lock:
            self.objects[key] = os.path.getsize(path)
            self.meta[key] = ExtraArgs or {}

    def list_objects_v2(self, Bucket, Prefix="", Delimiter=None, ContinuationToken=None):
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        if Delimiter:
            prefixes = sorted({Prefix + k[len(Prefix):].split(Delimiter)[0] + Delimiter
                               for k in keys if Delimiter in k[len(Prefix):]})
            return {"CommonPrefixes": [{"Prefix": p} for p in prefixes], "IsTruncated": False}
        return {"Contents": [{"Key": k, "Size": self.objects[k]} for k in keys], "IsTruncated": False}


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe not installed")
class UploadFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="hls_flow_")
        source = os.path.join(cls.tmp, "clip.mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25:duration=8",
                        "-f", "lavfi", "-i", "sine=duration=8", "-c:v", "libx264", "-preset", "ultrafast",
                        "-c:a", "aac", "-shortest", source], check=True)
        args = argparse.Namespace(out_dir=os.path.join(cls.tmp, "out"), name="demo", version=None, only=None,
                                  preset="ultrafast", force=False)
        cls.version_dir, cls.slug, cls.version = make_hls.build_hls(source, args)
        # junk that must never be uploaded
        with open(os.path.join(cls.version_dir, ".DS_Store"), "w") as handle:
            handle.write("x")
        with open(os.path.join(cls.version_dir, "360p", "360p.encode.json"), "w") as handle:
            handle.write("{}")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def start_server(self, docroot):
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=docroot)
        handler.log_message = lambda *a, **k: None
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def settings(self, base):
        return {"bucket": "test", "public_base": base}

    def test_full_upload_ordering_headers_allowlist_and_remote_validation(self):
        docroot = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, docroot, True)
        client = FakeS3(docroot)
        url = r2_upload.upload_version(client, self.settings(self.start_server(docroot)), self.version_dir,
                                       BASE, self.version)
        self.assertTrue(url.endswith(f"/{BASE}/v{self.version}/master.m3u8"))
        self.assertEqual(client.order[-1], r2_upload.key_for(BASE, self.version, "master.m3u8"))
        self.assertTrue(all(k.startswith(f"{BASE}/v{self.version}/") for k in client.objects))
        self.assertFalse(any(k.startswith("hls/") for k in client.objects))
        uploaded = {k.split(f"v{self.version}/", 1)[1] for k in client.objects}
        self.assertNotIn(".DS_Store", uploaded)
        self.assertNotIn("360p/360p.encode.json", uploaded)
        self.assertEqual(uploaded, {rel for rel, _, _ in r2_upload.collect_files(self.version_dir)[0]})
        for key, extra in client.meta.items():
            self.assertEqual(extra["CacheControl"], r2_upload.IMMUTABLE_CACHE, key)
            self.assertEqual(extra["ContentType"], r2_upload.content_type(key), key)
        # playlists are uploaded after every segment/init
        playlist_positions = [i for i, k in enumerate(client.order) if k.endswith(".m3u8")]
        media_positions = [i for i, k in enumerate(client.order) if k.endswith((".m4s", "init.mp4"))]
        self.assertLess(max(media_positions), min(playlist_positions))

    def test_published_version_is_immutable(self):
        docroot = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, docroot, True)
        client = FakeS3(docroot)
        settings = self.settings(self.start_server(docroot))
        r2_upload.upload_version(client, settings, self.version_dir, BASE, self.version)
        before = dict(client.objects)
        with self.assertRaises(RuntimeError) as ctx:
            r2_upload.upload_version(client, settings, self.version_dir, BASE, self.version, resume=True)
        self.assertIn("already published", str(ctx.exception))
        self.assertEqual(client.objects, before)

    def test_failed_upload_never_commits_and_resume_completes(self):
        docroot = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, docroot, True)
        settings = self.settings(self.start_server(docroot))
        client = FakeS3(docroot, fail_after=4)
        with self.assertRaises(Exception):
            r2_upload.upload_version(client, settings, self.version_dir, BASE, self.version)
        master_key = r2_upload.key_for(BASE, self.version, "master.m3u8")
        self.assertNotIn(master_key, client.objects)                       # no commit marker on failure
        self.assertTrue(client.objects)                                    # but some files did land

        with self.assertRaises(RuntimeError) as ctx:                       # re-upload without --resume refused
            r2_upload.upload_version(client, settings, self.version_dir, BASE, self.version)
        self.assertIn("--resume", str(ctx.exception))

        client.fail_after = None
        already = set(client.objects)
        client.order.clear()
        r2_upload.upload_version(client, settings, self.version_dir, BASE, self.version, resume=True)
        self.assertIn(master_key, client.objects)
        self.assertFalse(already & set(client.order))                      # resume only sent what was missing

    def test_dry_run_touches_nothing(self):
        docroot = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, docroot, True)
        client = FakeS3(docroot)
        result = r2_upload.upload_version(client, self.settings(""), self.version_dir, BASE, self.version,
                                          dry_run=True)
        self.assertIsNone(result)
        self.assertEqual(client.objects, {})

    def test_master_declares_closed_captions_none_and_validator_enforces_it(self):
        # Found by Apple's mediastreamvalidator: MUST declare CLOSED-CAPTIONS=NONE without caption renditions.
        master_path = os.path.join(self.version_dir, "master.m3u8")
        with open(master_path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertEqual(text.count("CLOSED-CAPTIONS=NONE"), text.count("#EXT-X-STREAM-INF"))
        import validate_hls
        copy = os.path.join(self.tmp, "no_cc")
        shutil.copytree(self.version_dir, copy)
        with open(os.path.join(copy, "master.m3u8"), "w", encoding="utf-8") as handle:
            handle.write(text.replace(",CLOSED-CAPTIONS=NONE", ""))
        report = validate_hls.validate_local(copy, deep_decode=False)
        self.assertFalse(report.ok)
        self.assertTrue(any("CLOSED-CAPTIONS" in e for e in report.errors))

    def test_remote_versions_listing(self):
        docroot = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, docroot, True)
        client = FakeS3(docroot)
        client.objects = {f"{BASE}/v1/master.m3u8": 1, f"{BASE}/v3/360p/seg_00000.m4s": 1,
                          "exercises/demo/other_clip/v9/x": 1, "kick_tracker/v5/x": 1}
        self.assertEqual(r2_upload.remote_versions(client, "test", BASE), [1, 3])

    def test_remote_validation_catches_a_missing_file(self):
        docroot = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, docroot, True)
        client = FakeS3(docroot)
        base = self.start_server(docroot)
        r2_upload.upload_version(client, self.settings(base), self.version_dir, BASE, self.version)
        os.remove(os.path.join(docroot, *BASE.split("/"), f"v{self.version}", "720p", "seg_00000.m4s"))
        import validate_hls
        report = validate_hls.validate_remote(f"{base}/{BASE}/v{self.version}/master.m3u8",
                                              local_dir=self.version_dir)
        self.assertFalse(report.ok)


if __name__ == "__main__":
    unittest.main()
