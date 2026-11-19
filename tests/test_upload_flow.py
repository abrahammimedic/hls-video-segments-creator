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
                                  preset="ultrafast", force=False, kind="short", segment_seconds=None)
        cls.version_dir, cls.slug, cls.version = make_hls.build_hls(source, args)
        # A short video is cut in 2 s pieces: an 8 s clip is 4 segments per rendition, each on a keyframe boundary.
        with open(os.path.join(cls.version_dir, "720p", "index.m3u8"), encoding="utf-8") as handle:
            playlist = handle.read()
        assert "#EXT-X-TARGETDURATION:2" in playlist and playlist.count("#EXTINF:2.0") == 4, playlist
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

    def test_a_built_version_has_a_poster_and_it_is_uploaded_before_the_master(self):
        poster_path = os.path.join(self.version_dir, "poster.jpg")
        self.assertTrue(os.path.isfile(poster_path), "build_hls makes the poster")
        self.assertEqual(open(poster_path, "rb").read(3), b"\xff\xd8\xff")
        allowed, _ = r2_upload.collect_files(self.version_dir)
        order = [rel for rel, _, _ in allowed]
        self.assertIn("poster.jpg", order)
        self.assertLess(order.index("poster.jpg"), order.index("master.m3u8"))

    def test_a_long_video_is_really_cut_in_6_s_pieces(self):
        source = os.path.join(self.tmp, "clip.mp4")
        args = argparse.Namespace(out_dir=os.path.join(self.tmp, "out_long"), name="demo_long", version=None,
                                  only="360p", preset="ultrafast", force=False, kind="long", segment_seconds=None)
        version_dir, _, _ = make_hls.build_hls(source, args)
        with open(os.path.join(version_dir, "360p", "index.m3u8"), encoding="utf-8") as handle:
            playlist = handle.read()
        self.assertIn("#EXT-X-TARGETDURATION:6", playlist)
        self.assertIn("#EXTINF:6.0", playlist)
        self.assertEqual(playlist.count("#EXTINF:"), 2)   # 8 s = 6 s + 2 s

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

    def test_skip_remote_check_for_token_protected_domains(self):
        # A domain that 403s plain requests must not turn a good upload into a failure.
        docroot = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, docroot, True)
        dead = {"bucket": "test", "public_base": "http://127.0.0.1:9"}     # nothing listens here
        client = FakeS3(docroot)
        url = r2_upload.upload_version(client, dead, self.version_dir, BASE, self.version, skip_remote_check=True)
        self.assertTrue(url.endswith(f"/{BASE}/v{self.version}/master.m3u8"))
        self.assertIn(r2_upload.key_for(BASE, self.version, "master.m3u8"), client.objects)
        # ...whereas without the flag the same unreachable domain is reported as a failed remote validation.
        other = FakeS3(tempfile.mkdtemp())
        with self.assertRaises(RuntimeError):
            r2_upload.upload_version(other, dead, self.version_dir, BASE, self.version)

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



class KindRouting(unittest.TestCase):
    """make_hls.main sends a short video to the Moments bucket and a long one to the protected bucket.

    Encoding and uploading are mocked, so this needs no ffmpeg and no R2.
    """

    ENV = {"R2_ACCOUNT_ID": "a", "R2_ACCESS_KEY_ID": "k", "R2_SECRET_ACCESS_KEY": "s",
           "R2_BUCKET": "exercise-bucket", "R2_PUBLIC_BASE_URL": "https://videos.example.com",
           "R2_MOMENTS_BUCKET": "moments-bucket", "R2_MOMENTS_PUBLIC_BASE_URL": "https://moments.example.com"}

    def run_main(self, argv, env=None):
        import unittest.mock as mock
        built, uploads = [], []

        def fake_build(path, args, remote_ctx=None):
            built.append((os.path.basename(path), args.kind, args.segment_seconds))
            return "/tmp/out/v1", "slug", 1

        def fake_upload(client, settings, version_dir, base, version, **kw):
            uploads.append((settings["bucket"], kw))
            return "https://x/master.m3u8"

        with tempfile.TemporaryDirectory() as tmp:
            files = []
            for name in ("reel.mp4", "lesson.mp4"):
                path = os.path.join(tmp, name)
                open(path, "wb").write(b"x")
                files.append(path)
            args = ["make_hls.py"] + [f for f in files if os.path.basename(f) in argv[0]] + argv[1:]
            with mock.patch.object(sys, "argv", args), \
                 mock.patch.dict(os.environ, env or self.ENV, clear=True), \
                 mock.patch("shutil.which", return_value="/usr/bin/x"), \
                 mock.patch.object(make_hls, "build_hls", fake_build), \
                 mock.patch.object(r2_upload, "make_client", lambda s: object()), \
                 mock.patch.object(r2_upload, "upload_version", fake_upload):
                make_hls.main()
        return built, uploads

    def test_a_short_video_is_cut_in_2_s_and_goes_to_the_moments_bucket(self):
        built, uploads = self.run_main(["reel.mp4", "--kind", "short", "--upload", "--remote-path", "mi_reels/reel"])
        self.assertEqual(built, [("reel.mp4", "short", 2)])
        bucket, kw = uploads[0]
        self.assertEqual(bucket, "moments-bucket")
        self.assertFalse(kw["skip_remote_check"], "the Moments domain is public, so it is checked")
        self.assertEqual(kw["kind"], "short")

    def test_a_long_video_is_cut_in_6_s_goes_to_the_protected_bucket_and_skips_the_public_check(self):
        built, uploads = self.run_main(["lesson.mp4", "--kind", "long", "--upload", "--remote-path", "exercises/lesson"])
        self.assertEqual(built, [("lesson.mp4", "long", 6)])
        bucket, kw = uploads[0]
        self.assertEqual(bucket, "exercise-bucket")
        self.assertTrue(kw["skip_remote_check"], "the token gate would refuse the check, so it is skipped by itself")
        self.assertEqual(kw["kind"], "long")

    def test_an_explicit_segment_length_still_wins(self):
        built, _ = self.run_main(["reel.mp4", "--kind", "short", "--segment-seconds", "4"])
        self.assertEqual(built, [("reel.mp4", "short", 4)])

    def test_the_question_is_asked_for_each_video_before_any_encoding(self):
        import unittest.mock as mock
        order = []
        answers = iter(["s", "l"])

        def fake_build(path, args, remote_ctx=None):
            order.append(("build", args.kind))
            return "/tmp/out/v1", "slug", 1

        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for name in ("a.mp4", "b.mp4"):
                path = os.path.join(tmp, name)
                open(path, "wb").write(b"x")
                paths.append(path)

            def fake_ask(question):
                order.append(("ask", os.path.basename(paths[len([o for o in order if o[0] == "ask"])])))
                return next(answers)

            with mock.patch.object(sys, "argv", ["make_hls.py"] + paths), \
                 mock.patch("sys.stdin.isatty", return_value=True), \
                 mock.patch("builtins.input", fake_ask), \
                 mock.patch("shutil.which", return_value="/usr/bin/x"), \
                 mock.patch.object(make_hls, "build_hls", fake_build):
                make_hls.main()
        self.assertEqual(order, [("ask", "a.mp4"), ("ask", "b.mp4"), ("build", "short"), ("build", "long")])

    def test_without_a_terminal_and_without_kind_it_stops_before_encoding(self):
        import unittest.mock as mock
        built = []
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.mp4")
            open(path, "wb").write(b"x")
            with mock.patch.object(sys, "argv", ["make_hls.py", path]), \
                 mock.patch("sys.stdin.isatty", return_value=False), \
                 mock.patch("shutil.which", return_value="/usr/bin/x"), \
                 mock.patch.object(make_hls, "build_hls", lambda *a, **k: built.append(a)):
                with self.assertRaises(SystemExit) as ctx:
                    make_hls.main()
        self.assertIn("--kind", str(ctx.exception))
        self.assertEqual(built, [])

    def test_missing_input_files_stop_with_a_clear_message(self):
        import unittest.mock as mock
        with mock.patch.object(sys, "argv", ["make_hls.py", "/nope/a.mp4", "--kind", "short", "--upload",
                                             "--remote-path", "mi_reels/a"]), \
             mock.patch("shutil.which", return_value="/usr/bin/x"):
            with self.assertRaises(SystemExit) as ctx:
                make_hls.main()
        self.assertIn("none of the input files exist", str(ctx.exception))

    def test_a_short_upload_without_the_moments_bucket_stops_before_encoding(self):
        env = {k: v for k, v in self.ENV.items() if k != "R2_MOMENTS_BUCKET"}
        with self.assertRaises(SystemExit) as ctx:
            self.run_main(["reel.mp4", "--kind", "short", "--upload", "--remote-path", "mi_reels/reel"], env=env)
        self.assertIn("R2_MOMENTS_BUCKET", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
