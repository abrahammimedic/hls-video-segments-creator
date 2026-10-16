"""Unit tests for the pure logic of the HLS tools. Run: python3 -m unittest discover tests"""

import os
import sys
import tempfile
import unittest
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hls_common as common   # noqa: E402
import make_hls               # noqa: E402
import r2_upload              # noqa: E402


class SegmentBitRates(unittest.TestCase):
    """RFC 8216 4.1 / 4.3.4.2: peak = best contiguous run lasting 0.5x-1.5x TARGETDURATION."""

    def test_uniform_segments(self):
        peak, average = common.segment_bit_rates([750_000] * 5, [6.0] * 5, 6)
        self.assertAlmostEqual(peak, 1_000_000)
        self.assertAlmostEqual(average, 1_000_000)

    def test_short_tail_is_grouped_with_its_neighbour(self):
        # Tail: 2 s alone is under 0.5x target, so it cannot be a window by itself.
        # A naive "largest segment" rule would report 400000 bytes / 2 s = 1.6 Mbps.
        peak, average = common.segment_bit_rates([600_000, 600_000, 400_000], [6.0, 6.0, 2.0], 6)
        self.assertAlmostEqual(peak, 8 * 1_000_000 / 8)           # [6 s, 2 s] = 8 s window -> 1.0 Mbps
        self.assertAlmostEqual(average, 8 * 1_600_000 / 14)

    def test_spike_segment_sets_the_peak(self):
        peak, _ = common.segment_bit_rates([100_000, 900_000, 100_000], [6.0] * 3, 6)
        self.assertAlmostEqual(peak, 8 * 900_000 / 6)

    def test_ntsc_segments(self):
        peak, _ = common.segment_bit_rates([750_750] * 3, [6.006] * 3, 6)
        self.assertAlmostEqual(peak, 8 * 750_750 / 6.006)

    def test_two_short_segments_qualify_together(self):
        # 2 s + 2 s = 4 s is within 3-9 s, 2 s alone is not.
        peak, _ = common.segment_bit_rates([100, 400], [2.0, 2.0], 6)
        self.assertAlmostEqual(peak, 8 * 500 / 4)

    def test_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            common.segment_bit_rates([], [], 6)
        with self.assertRaises(ValueError):
            common.segment_bit_rates([1, 2], [6.0], 6)


class Codecs(unittest.TestCase):
    def test_avcc_to_codec_string(self):
        init = b"\x00\x00\x00\x20ftypiso5...avcC" + bytes([1, 0x4D, 0x40, 0x1F]) + b"\xff\xe1"
        self.assertEqual(common.avc_codec_string(init), "avc1.4D401F")

    def test_missing_avcc(self):
        with self.assertRaises(ValueError):
            common.avc_codec_string(b"no codec box here")


class Playlists(unittest.TestCase):
    MEDIA = ("#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-TARGETDURATION:6\n#EXT-X-PLAYLIST-TYPE:VOD\n"
             "#EXT-X-INDEPENDENT-SEGMENTS\n#EXT-X-MAP:URI=\"init.mp4\"\n#EXTINF:6.000000,\nseg_00000.m4s\n"
             "#EXTINF:2.000000,\nseg_00001.m4s\n#EXT-X-ENDLIST\n")

    def test_media_playlist(self):
        info = common.parse_media_playlist(self.MEDIA)
        self.assertEqual(info["version"], 7)
        self.assertEqual(info["map_uri"], "init.mp4")
        self.assertTrue(info["endlist"] and info["independent"])
        self.assertEqual(info["segments"], [(6.0, "seg_00000.m4s"), (2.0, "seg_00001.m4s")])
        self.assertEqual(info["discontinuities"], 0)

    def test_master_playlist_with_commas_in_quotes(self):
        text = ('#EXTM3U\n#EXT-X-INDEPENDENT-SEGMENTS\n'
                '#EXT-X-STREAM-INF:BANDWIDTH=543609,AVERAGE-BANDWIDTH=532824,CODECS="avc1.42C01E,mp4a.40.2",'
                'RESOLUTION=640x360,FRAME-RATE=25.000\n360p/index.m3u8\n')
        master = common.parse_master_playlist(text)
        self.assertTrue(master["independent"])
        variant = master["variants"][0]
        self.assertEqual(variant["CODECS"], "avc1.42C01E,mp4a.40.2")
        self.assertEqual(variant["RESOLUTION"], "640x360")
        self.assertEqual(variant["URI"], "360p/index.m3u8")


class FrameRate(unittest.TestCase):
    def test_standard_rates_are_preserved(self):
        for source, expected in [(Fraction(24000, 1001), Fraction(24000, 1001)), (Fraction(24), Fraction(24)),
                                 (Fraction(25), Fraction(25)), (Fraction(30000, 1001), Fraction(30000, 1001)),
                                 (Fraction(30), Fraction(30))]:
            self.assertEqual(make_hls.choose_fps(source), expected)

    def test_near_standard_snaps(self):
        self.assertEqual(make_hls.choose_fps(Fraction(2997, 100)), Fraction(30000, 1001))
        self.assertEqual(make_hls.choose_fps(Fraction(23976, 1000)), Fraction(24000, 1001))

    def test_high_rates_are_halved(self):
        self.assertEqual(make_hls.choose_fps(Fraction(60)), Fraction(30))
        self.assertEqual(make_hls.choose_fps(Fraction(60000, 1001)), Fraction(30000, 1001))
        self.assertEqual(make_hls.choose_fps(Fraction(50)), Fraction(25))

    def test_gop_is_two_seconds(self):
        self.assertEqual(make_hls.gop_frames(Fraction(24000, 1001)), 48)
        self.assertEqual(make_hls.gop_frames(Fraction(25)), 50)
        self.assertEqual(make_hls.gop_frames(Fraction(30000, 1001)), 60)
        self.assertEqual(make_hls.gop_frames(Fraction(30)), 60)


class LadderSelection(unittest.TestCase):
    def dims(self, w, h, only=None):
        return [(r.name, r.width, r.height) for r in make_hls.select_renditions(w, h, only)]

    def test_landscape_1080p(self):
        self.assertEqual(self.dims(1920, 1080), [("360p", 640, 360), ("720p", 1280, 720), ("1080p", 1920, 1080)])

    def test_portrait_stays_portrait(self):
        self.assertEqual(self.dims(1080, 1920), [("360p", 360, 640), ("720p", 720, 1280), ("1080p", 1080, 1920)])
        self.assertEqual(self.dims(2160, 3840)[0], ("360p", 360, 640))

    def test_never_upscales(self):
        self.assertEqual(self.dims(1280, 720), [("360p", 640, 360), ("720p", 1280, 720)])
        self.assertEqual(self.dims(854, 480), [("360p", 640, 360)])

    def test_aspect_ratio_preserved_for_4_3(self):
        self.assertEqual(self.dims(1600, 1200), [("360p", 480, 360), ("720p", 960, 720), ("1080p", 1440, 1080)])

    def test_near_1080_source_gets_1080_rendition_at_source_size(self):
        self.assertEqual(self.dims(1920, 1072)[-1], ("1080p", 1920, 1072))

    def test_source_below_lowest_step_gets_single_native_rendition(self):
        renditions = make_hls.select_renditions(426, 240)
        self.assertEqual([(r.name, r.width, r.height) for r in renditions], [("240p", 426, 240)])
        self.assertLess(renditions[0].kbps, 400)                     # bitrate scaled down with pixel count
        self.assertGreaterEqual(renditions[0].kbps, make_hls.NATIVE_MIN_KBPS)

    def test_tiny_source_hits_bitrate_floor_and_stays_even(self):
        (r,) = make_hls.select_renditions(161, 121)
        self.assertEqual(r.kbps, make_hls.NATIVE_MIN_KBPS)
        self.assertEqual((r.width % 2, r.height % 2), (0, 0))

    def test_only_filter(self):
        self.assertEqual([n for n, _, _ in self.dims(1920, 1080, "360p,720p")], ["360p", "720p"])
        with self.assertRaises(ValueError):
            make_hls.select_renditions(1280, 720, "1080p")

    def test_h264_levels(self):
        fps = Fraction(30)
        self.assertEqual(make_hls.pick_level(640, 360, fps), "3.0")
        self.assertEqual(make_hls.pick_level(1280, 720, fps), "3.1")
        self.assertEqual(make_hls.pick_level(1920, 1080, fps), "4.0")


class Versioning(unittest.TestCase):
    def test_versions(self):
        with tempfile.TemporaryDirectory() as slug_dir:
            self.assertEqual(make_hls.choose_version(slug_dir), 1)
            os.makedirs(os.path.join(slug_dir, "v1"))
            self.assertEqual(make_hls.choose_version(slug_dir), 1)            # unfinished: resume it
            open(os.path.join(slug_dir, "v1", "master.m3u8"), "w").close()
            self.assertEqual(make_hls.choose_version(slug_dir), 2)            # built: never reuse
            self.assertEqual(make_hls.choose_version(slug_dir, remote=[1, 2, 3]), 4)
            self.assertEqual(make_hls.choose_version(slug_dir, requested=7), 7)

    def test_unfinished_local_version_that_exists_remotely_is_skipped(self):
        with tempfile.TemporaryDirectory() as slug_dir:
            os.makedirs(os.path.join(slug_dir, "v1"))
            self.assertEqual(make_hls.choose_version(slug_dir, remote=[1]), 2)

    def test_slugify(self):
        self.assertEqual(make_hls.slugify("Trimester 2 yoga_video (final).mp4"), "trimester-2-yoga-video-final-mp4")
        self.assertEqual(make_hls.slugify("!!!"), "video")


class FfmpegCommands(unittest.TestCase):
    def setUp(self):
        self.r = make_hls.Rendition("720p", 1280, 720, 1100)
        self.fps = Fraction(25)

    def test_video_filter_forces_constant_frame_rate_without_timestamp_resets(self):
        flt = make_hls.video_filter(self.r, Fraction(30000, 1001))
        self.assertIn("fps=30000/1001", flt)
        self.assertNotIn("setpts", flt)

    def test_with_audio_maps_audio_optionally(self):
        cmd = make_hls.pass2_command("in.mp4", self.r, self.fps, "slow", "log", "/tmp/o", True)
        self.assertIn("0:a:0?", cmd)
        self.assertIn("aac", cmd)
        self.assertIn("-g", cmd)
        self.assertEqual(cmd[cmd.index("-g") + 1], "50")
        self.assertIn("independent_segments", cmd)
        self.assertNotIn("setpts", " ".join(cmd))
        self.assertNotIn("asetpts", " ".join(cmd))

    def test_silent_source_has_no_audio_arguments(self):
        cmd = make_hls.pass2_command("in.mp4", self.r, self.fps, "slow", "log", "/tmp/o", False)
        self.assertNotIn("0:a:0?", cmd)
        self.assertNotIn("-c:a", cmd)


class Completeness(unittest.TestCase):
    PLAYLIST = ("#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-TARGETDURATION:6\n#EXT-X-MAP:URI=\"init.mp4\"\n"
                "#EXTINF:6.0,\nseg_00000.m4s\n#EXTINF:4.0,\nseg_00001.m4s\n#EXT-X-ENDLIST\n")

    def build(self, directory, playlist=None, skip=(), empty=()):
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "index.m3u8"), "w") as handle:
            handle.write(playlist or self.PLAYLIST)
        for name in ("init.mp4", "seg_00000.m4s", "seg_00001.m4s"):
            if name not in skip:
                with open(os.path.join(directory, name), "wb") as handle:
                    handle.write(b"" if name in empty else b"data")

    def test_complete(self):
        with tempfile.TemporaryDirectory() as d:
            self.build(d)
            self.assertTrue(make_hls.rendition_is_complete(d, 10.0))

    def test_incomplete_variants(self):
        with tempfile.TemporaryDirectory() as d:
            self.build(d, skip=("seg_00001.m4s",))
            self.assertFalse(make_hls.rendition_is_complete(d, 10.0))
        with tempfile.TemporaryDirectory() as d:
            self.build(d, empty=("seg_00000.m4s",))
            self.assertFalse(make_hls.rendition_is_complete(d, 10.0))
        with tempfile.TemporaryDirectory() as d:
            self.build(d, playlist=self.PLAYLIST.replace("#EXT-X-ENDLIST\n", ""))
            self.assertFalse(make_hls.rendition_is_complete(d, 10.0))
        with tempfile.TemporaryDirectory() as d:
            self.build(d)
            self.assertFalse(make_hls.rendition_is_complete(d, 600.0))    # duration far from the source
        self.assertFalse(make_hls.rendition_is_complete("/nonexistent/dir", 10.0))

    def test_fingerprint_changes_with_settings(self):
        with tempfile.NamedTemporaryFile(suffix=".mp4") as source:
            source.write(b"x")
            source.flush()
            base = make_hls.fingerprint(source.name, self.r(1100), Fraction(25), "slow", True)
            self.assertEqual(base, make_hls.fingerprint(source.name, self.r(1100), Fraction(25), "slow", True))
            self.assertNotEqual(base, make_hls.fingerprint(source.name, self.r(1500), Fraction(25), "slow", True))
            self.assertNotEqual(base, make_hls.fingerprint(source.name, self.r(1100), Fraction(25), "fast", True))
            self.assertNotEqual(base, make_hls.fingerprint(source.name, self.r(1100), Fraction(30), "slow", True))
            self.assertNotEqual(base, make_hls.fingerprint(source.name, self.r(1100), Fraction(25), "slow", False))

    @staticmethod
    def r(kbps):
        return make_hls.Rendition("720p", 1280, 720, kbps)


class UploadRules(unittest.TestCase):
    ALLOWED = ["master.m3u8", "360p/index.m3u8", "360p/init.mp4", "360p/seg_00000.m4s",
               "720p/index.m3u8", "1080p/seg_01234.m4s"]
    BLOCKED = [".DS_Store", "publish.json", "360p/360p.encode.json", "360p.encode.json", ".work/v1/publish.json",
               "360p.partial/seg_00000.m4s", "360p/index.m3u8.bak", "360p/seg_1.m4s", "ffmpeg2pass-0.log",
               "360p/notes.txt", "master.m3u8.tmp", "../master.m3u8"]

    def test_classify(self):
        for rel in self.ALLOWED:
            self.assertIsNotNone(r2_upload.classify(rel), rel)
        for rel in self.BLOCKED:
            self.assertIsNone(r2_upload.classify(rel), rel)

    def test_collect_files_uploads_only_the_allowlist(self):
        with tempfile.TemporaryDirectory() as root:
            for rel in self.ALLOWED + [b for b in self.BLOCKED if not b.startswith("..")]:
                path = os.path.join(root, rel)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w") as handle:
                    handle.write("x")
            allowed, excluded = r2_upload.collect_files(root)
            self.assertEqual({rel for rel, _, _ in allowed}, set(self.ALLOWED))
            for rel in ("publish.json", "360p/360p.encode.json", ".DS_Store", ".work/v1/publish.json"):
                self.assertIn(rel, excluded)
            kinds = [kind for _, _, kind in allowed]
            self.assertEqual(kinds[-1], "master")                           # commit marker goes last
            self.assertLess(max(i for i, k in enumerate(kinds) if k in ("segment", "init")),
                            min(i for i, k in enumerate(kinds) if k == "playlist"))

    def test_content_types(self):
        self.assertEqual(r2_upload.content_type("master.m3u8"), "application/vnd.apple.mpegurl")
        self.assertEqual(r2_upload.content_type("360p/seg_00000.m4s"), "video/iso.segment")
        self.assertEqual(r2_upload.content_type("360p/init.mp4"), "video/mp4")
        with self.assertRaises(ValueError):
            r2_upload.content_type("publish.json")

    def test_keys_and_versions(self):
        base = "exercises/trimester_2/trimester_2_yoga"
        self.assertEqual(r2_upload.key_for(base, 3, "master.m3u8"), f"{base}/v3/master.m3u8")
        self.assertEqual(r2_upload.parse_version_prefixes(
            [f"{base}/v1/", f"{base}/v12/", f"{base}/vx/", f"{base}/notes/", "exercises/trimester_2/other/v7/"], base),
            [1, 12])

    def test_bucket_path_validation(self):
        self.assertEqual(r2_upload.normalize_base_path("/exercises/trimester_2/x_y/"), "exercises/trimester_2/x_y")
        for bad in ["", "/", "..", "a/../b", "a//b", "a b", "a/b?x", "exercises/./x", None]:
            with self.assertRaises(ValueError, msg=repr(bad)):
                r2_upload.normalize_base_path(bad)

    def test_settings(self):
        with self.assertRaises(RuntimeError) as ctx:
            r2_upload.load_settings({"R2_BUCKET": "b"})
        self.assertIn("R2_ACCOUNT_ID", str(ctx.exception))
        self.assertNotIn("R2_BUCKET", str(ctx.exception))
        settings = r2_upload.load_settings({"R2_ACCOUNT_ID": "a", "R2_ACCESS_KEY_ID": "k",
                                            "R2_SECRET_ACCESS_KEY": "s", "R2_BUCKET": "b",
                                            "R2_PUBLIC_BASE_URL": "https://cdn.example.com/"})
        self.assertEqual(settings["public_base"], "https://cdn.example.com")


if __name__ == "__main__":
    unittest.main()
