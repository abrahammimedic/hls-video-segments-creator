"""The poster (thumbnail): which frame is picked, its size, and adding one to a published version.

The frame tests make tiny real videos with ffmpeg, so they are skipped when ffmpeg is unavailable.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import make_hls     # noqa: E402
import poster       # noqa: E402
import r2_upload    # noqa: E402

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def make_video(path, size, seconds, black_first=0):
    """A test-pattern video, optionally black for the first `black_first` seconds."""
    width, height = size
    if black_first:
        graph = (f"color=c=black:s={width}x{height}:d={black_first}:r=25[a];"
                 f"testsrc2=s={width}x{height}:d={seconds - black_first}:r=25[b];[a][b]concat=n=2:v=1:a=0")
        cmd = ["ffmpeg", "-v", "error", "-y", "-filter_complex", graph, "-pix_fmt", "yuv420p", path]
    else:
        cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=s={width}x{height}:d={seconds}:r=25",
               "-pix_fmt", "yuv420p", path]
    subprocess.run(cmd, check=True)


class Pure(unittest.TestCase):
    def test_portrait_keeps_the_short_side_at_540(self):
        self.assertEqual(poster.poster_size(1080, 1920), (540, 960))

    def test_landscape_keeps_the_short_side_at_540(self):
        self.assertEqual(poster.poster_size(1920, 1080), (960, 540))

    def test_a_small_source_is_never_upscaled(self):
        self.assertEqual(poster.poster_size(360, 640), (360, 640))
        self.assertEqual(poster.poster_size(640, 360), (640, 360))

    def test_sizes_are_even(self):
        for w, h in ((1079, 1919), (1000, 1777), (1921, 1081)):
            pw, ph = poster.poster_size(w, h)
            self.assertEqual((pw % 2, ph % 2), (0, 0), (w, h))

    def test_an_explicit_time_is_the_only_candidate(self):
        self.assertEqual(poster.candidate_starts(80, at=12.5), [12.5])

    def test_automatic_candidates_are_spread_through_the_video(self):
        self.assertEqual(poster.candidate_starts(80), [1.0, 20.0, 40.0])

    def test_a_very_short_video_has_one_candidate_at_the_start(self):
        self.assertEqual(poster.candidate_starts(2), [0.0])

    def test_candidates_stay_inside_the_video(self):
        for start in poster.candidate_starts(5):
            self.assertLessEqual(start, 4.0)

    def test_the_command_picks_the_best_frame_only_when_asked(self):
        auto = poster.frame_command("in.mp4", 1.0, (540, 960), "o.jpg", 4, pick_best=True)
        self.assertIn("-t", auto)
        self.assertTrue(any("thumbnail=" in part for part in auto))
        exact = poster.frame_command("in.mp4", 7.0, (540, 960), "o.jpg", 4, pick_best=False)
        self.assertNotIn("-t", exact)
        self.assertFalse(any("thumbnail=" in part for part in exact))
        self.assertEqual(exact[exact.index("-ss") + 1], "7.000")
        self.assertIn("-map_metadata", exact)       # no location or device data in a public image
        self.assertEqual(exact[exact.index("-frames:v") + 1], "1")


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class Frames(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="poster_test_")
        cls.portrait = os.path.join(cls.tmp, "portrait.mp4")
        cls.landscape = os.path.join(cls.tmp, "landscape.mp4")
        cls.black_start = os.path.join(cls.tmp, "black_start.mp4")
        cls.all_black = os.path.join(cls.tmp, "all_black.mp4")
        make_video(cls.portrait, (360, 640), 8)
        make_video(cls.landscape, (640, 360), 8)
        make_video(cls.black_start, (360, 640), 12, black_first=5)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=360x640:d=6:r=25",
                        "-pix_fmt", "yuv420p", cls.all_black], check=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def make(self, path, w, h, duration, name="p.jpg", **kw):
        out = os.path.join(self.tmp, name)
        info = poster.make_poster(path, w, h, duration, out, log=lambda *_: None, **kw)
        return out, info

    def test_writes_a_jpeg_at_the_poster_size(self):
        out, info = self.make(self.portrait, 360, 640, 8)
        self.assertEqual(info["size"], (360, 640))
        self.assertTrue(open(out, "rb").read(3) == b"\xff\xd8\xff", "a JPEG")
        self.assertLessEqual(info["bytes"], poster.MAX_BYTES)

    def test_a_landscape_video_gets_a_landscape_poster(self):
        _, info = self.make(self.landscape, 640, 360, 8)
        self.assertEqual(info["size"], (640, 360))

    def test_the_pick_is_not_black_or_white(self):
        _, info = self.make(self.portrait, 360, 640, 8)
        self.assertGreaterEqual(info["luma"], poster.MIN_LUMA)
        self.assertLessEqual(info["luma"], poster.MAX_LUMA)

    def test_a_video_that_starts_black_does_not_get_a_black_poster(self):
        _, info = self.make(self.black_start, 360, 640, 12)
        self.assertGreaterEqual(info["luma"], poster.MIN_LUMA, "skipped the black opening")

    def test_an_explicit_time_is_used_exactly(self):
        _, info = self.make(self.black_start, 360, 640, 12, at=9.0)
        self.assertEqual(info["time"], 9.0)
        self.assertGreaterEqual(info["luma"], poster.MIN_LUMA)

    def test_an_explicit_time_inside_the_black_part_is_honoured(self):
        _, info = self.make(self.black_start, 360, 640, 12, at=2.0)
        self.assertEqual(info["time"], 2.0)
        self.assertLess(info["luma"], poster.MIN_LUMA, "the user asked for that frame")

    def test_a_time_past_the_end_is_refused(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.make(self.portrait, 360, 640, 8, at=30)
        self.assertIn("past the end", str(ctx.exception))

    def test_an_all_black_video_still_gets_a_poster_and_says_so(self):
        messages = []
        out = os.path.join(self.tmp, "black.jpg")
        info = poster.make_poster(self.all_black, 360, 640, 6, out, log=messages.append)
        self.assertTrue(os.path.isfile(out))
        self.assertLess(info["luma"], poster.MIN_LUMA)
        self.assertTrue(any("--poster-at" in m for m in messages), "tells you how to choose another")

    def test_a_missing_video_is_an_error_not_a_crash(self):
        with self.assertRaises(RuntimeError):
            self.make(os.path.join(self.tmp, "nope.mp4"), 360, 640, 8)

    def test_no_temp_files_are_left_behind(self):
        before = {d for d in os.listdir(tempfile.gettempdir()) if d.startswith("poster_")}
        self.make(self.portrait, 360, 640, 8)
        after = {d for d in os.listdir(tempfile.gettempdir()) if d.startswith("poster_")}
        self.assertEqual(after - before, set())


class FakeBucket:
    """The bits of a boto3 S3 client that upload_poster uses, over a dict."""

    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.uploads = []

    def list_objects_v2(self, Bucket, Prefix, **_):
        found = [{"Key": k, "Size": v} for k, v in sorted(self.objects.items()) if k.startswith(Prefix)]
        return {"Contents": found, "IsTruncated": False}

    def upload_file(self, path, bucket, key, ExtraArgs=None):
        self.objects[key] = os.path.getsize(path)
        self.uploads.append((key, ExtraArgs))


class AddingAPoster(unittest.TestCase):
    BASE = "mi_reels/clip"
    MASTER = "mi_reels/clip/v1/master.m3u8"
    POSTER = "mi_reels/clip/v1/poster.jpg"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="poster_up_")
        self.path = os.path.join(self.tmp, "poster.jpg")
        with open(self.path, "wb") as handle:
            handle.write(b"\xff\xd8\xff" + b"x" * 5000)
        self.settings = {"bucket": "moments", "public_base": ""}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_it_is_part_of_a_normal_upload(self):
        self.assertEqual(r2_upload.classify("poster.jpg"), "poster")
        self.assertEqual(r2_upload.content_type("poster.jpg"), "image/jpeg")
        self.assertLess(r2_upload.UPLOAD_ORDER["poster"], r2_upload.UPLOAD_ORDER["master"],
                        "the master playlist is the commit: the poster goes first")

    def test_only_a_file_with_that_exact_name_is_uploaded(self):
        for name in ("poster.png", "poster.jpg.bak", "sub/poster.jpg", "Poster.jpg"):
            self.assertIsNone(r2_upload.classify(name), name)

    def test_uploads_with_the_right_type_and_cache_headers(self):
        bucket = FakeBucket({self.MASTER: 556})
        r2_upload.upload_poster(bucket, self.settings, self.path, self.BASE, 1)
        key, extra = bucket.uploads[0]
        self.assertEqual(key, self.POSTER)
        self.assertEqual(extra["ContentType"], "image/jpeg")
        self.assertEqual(extra["CacheControl"], r2_upload.IMMUTABLE_CACHE)

    def test_refuses_a_version_that_is_not_published(self):
        bucket = FakeBucket()
        with self.assertRaises(RuntimeError) as ctx:
            r2_upload.upload_poster(bucket, self.settings, self.path, self.BASE, 1)
        self.assertIn("publish the video first", str(ctx.exception))
        self.assertEqual(bucket.uploads, [])

    def test_an_existing_poster_is_not_replaced_without_force(self):
        bucket = FakeBucket({self.MASTER: 556, self.POSTER: 100})
        with self.assertRaises(RuntimeError) as ctx:
            r2_upload.upload_poster(bucket, self.settings, self.path, self.BASE, 1)
        self.assertIn("--force", str(ctx.exception))
        self.assertEqual(bucket.uploads, [])
        r2_upload.upload_poster(bucket, self.settings, self.path, self.BASE, 1, force=True)
        self.assertEqual(len(bucket.uploads), 1)

    def test_a_dry_run_uploads_nothing(self):
        bucket = FakeBucket({self.MASTER: 556})
        r2_upload.upload_poster(bucket, self.settings, self.path, self.BASE, 1, dry_run=True)
        self.assertEqual(bucket.uploads, [])

    def test_it_never_touches_the_video_files(self):
        bucket = FakeBucket({self.MASTER: 556, "mi_reels/clip/v1/360p/seg_00000.m4s": 99})
        before = dict(bucket.objects)
        r2_upload.upload_poster(bucket, self.settings, self.path, self.BASE, 1)
        after = {k: v for k, v in bucket.objects.items() if k != self.POSTER}
        self.assertEqual(after, before)


class Options(unittest.TestCase):
    def parse(self, *argv):
        with mock.patch.object(sys, "argv", ["make_hls.py", "v.mp4", *argv]), \
             mock.patch("builtins.print"):
            return make_hls.parse_args()

    def test_defaults(self):
        args = self.parse()
        self.assertIsNone(args.poster_at)
        self.assertFalse(args.no_poster)
        self.assertFalse(args.poster_only)

    def test_poster_at_takes_seconds(self):
        self.assertEqual(self.parse("--poster-at", "12.5").poster_at, 12.5)

    def test_a_negative_time_is_refused(self):
        with self.assertRaises(SystemExit):
            self.parse("--poster-at", "-1")

    def test_poster_only_needs_upload_remote_path_and_version(self):
        for argv in (["--poster-only"],
                     ["--poster-only", "--upload", "--remote-path", "mi_reels/clip"],
                     ["--poster-only", "--version", "1"],
                     ["--poster-only", "--upload", "--version", "1"]):
            with self.assertRaises(SystemExit, msg=" ".join(argv)):
                self.parse(*argv)
        args = self.parse("--poster-only", "--upload", "--remote-path", "mi_reels/clip", "--version", "1")
        self.assertTrue(args.poster_only)

    def test_poster_only_and_no_poster_contradict(self):
        with self.assertRaises(SystemExit):
            self.parse("--poster-only", "--no-poster", "--upload", "--remote-path", "a/b", "--version", "1")

    def test_poster_only_takes_one_video(self):
        with mock.patch.object(sys, "argv", ["make_hls.py", "a.mp4", "b.mp4", "--poster-only", "--upload"]), \
             mock.patch("builtins.print"):
            with self.assertRaises(SystemExit):
                make_hls.parse_args()


class PosterOnlyRouting(unittest.TestCase):
    """make_hls.main with --poster-only adds a poster and never encodes or builds."""

    ENV = {"R2_ACCOUNT_ID": "a", "R2_ACCESS_KEY_ID": "k", "R2_SECRET_ACCESS_KEY": "s",
           "R2_BUCKET": "exercise-bucket", "R2_PUBLIC_BASE_URL": "https://videos.example.com",
           "R2_MOMENTS_BUCKET": "moments-bucket", "R2_MOMENTS_PUBLIC_BASE_URL": "https://moments.example.com"}

    def run_main(self, kind):
        calls = {"build": 0, "poster": []}
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "clip.mp4")
            open(src, "wb").write(b"x")
            argv = ["make_hls.py", src, "--kind", kind, "--poster-only", "--upload", "--remote-path", "mi_reels/clip",
                    "--version", "1"]
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.dict(os.environ, self.ENV, clear=True), \
                 mock.patch("shutil.which", return_value="/usr/bin/x"), \
                 mock.patch.object(r2_upload, "make_client", lambda s: object()), \
                 mock.patch.object(make_hls, "build_hls", lambda *a, **k: calls.__setitem__("build", calls["build"] + 1)), \
                 mock.patch.object(make_hls, "add_poster_to_published",
                                   lambda path, args, client, settings, k: calls["poster"].append((settings["bucket"], k))):
                make_hls.main()
        return calls

    def test_a_short_video_gets_its_poster_in_the_moments_bucket(self):
        calls = self.run_main("short")
        self.assertEqual(calls["build"], 0)
        self.assertEqual(calls["poster"], [("moments-bucket", "short")])

    def test_a_long_video_gets_its_poster_in_the_protected_bucket(self):
        calls = self.run_main("long")
        self.assertEqual(calls["poster"], [("exercise-bucket", "long")])


if __name__ == "__main__":
    unittest.main()
