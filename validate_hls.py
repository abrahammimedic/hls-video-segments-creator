#!/usr/bin/env python3
"""Validate an HLS (fMP4/CMAF) version folder, or a published version URL.

Local mode inspects the playlists and decodes the stream *through the playlist* (init segment +
media segments together), because a bare .m4s has no moov box and cannot be probed alone.

Usage:
    python3 validate_hls.py <out>/<slug>/v1            # deep local validation
    python3 validate_hls.py https://cdn.example.com/hls/<slug>/v1/master.m3u8 [--local <dir>]
Add --apple-validate to also run Apple's mediastreamvalidator if it is installed.
"""

import argparse
import functools
import http.server
import os
import random
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import hls_common as common

AAC_FRAME_SECONDS = 1024 / 48000
START_OFFSET_WARN = 0.100       # |audio start - video start| above this is only a warning
DRIFT_WARN, DRIFT_ERROR = 0.050, 0.250
SEGMENT_AV_WARN, SEGMENT_AV_ERROR = 2 * AAC_FRAME_SECONDS, 0.250
BANDWIDTH_TOLERANCE = 0.01
SEEK_SAMPLES = 10
PROBE_TIMEOUT, DECODE_TIMEOUT, SEEK_TIMEOUT = 900, 1800, 120


class Report:
    def __init__(self):
        self.errors, self.warnings, self.notes = [], [], []

    @property
    def ok(self):
        return not self.errors

    def error(self, message):
        self.errors.append(message)

    def warn(self, message):
        self.warnings.append(message)

    def note(self, message):
        self.notes.append(message)

    def print(self):
        print("\n   Validation")
        for message in self.notes:
            print(f"     ok    {message}")
        for message in self.warnings:
            print(f"     WARN  {message}")
        for message in self.errors:
            print(f"     FAIL  {message}")
        print(f"   Result: {'PASSED' if self.ok else 'FAILED'} "
              f"({len(self.errors)} errors, {len(self.warnings)} warnings)")


def read_text(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


# ------------------------------------------------------------ packet access

def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def probe_packets(playlist_path):
    """Return ({video: [(pts, dts, dur, is_key)]}, {audio: [(pts, dur)]}) read through the playlist."""
    cmd = ["ffprobe", "-v", "error", "-show_entries",
           "packet=codec_type,pts_time,dts_time,duration_time,flags", "-of", "compact", playlist_path]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=PROBE_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"ffprobe timed out after {PROBE_TIMEOUT}s")
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ffprobe failed")
    video, audio = [], []
    for line in result.stdout.splitlines():
        if not line.startswith("packet|"):
            continue
        fields = dict(item.split("=", 1) for item in line.split("|")[1:] if "=" in item)
        pts, dts, dur = _num(fields.get("pts_time")), _num(fields.get("dts_time")), _num(fields.get("duration_time"))
        if fields.get("codec_type") == "video":
            video.append((pts if pts is not None else dts, dts if dts is not None else pts, dur,
                          "K" in fields.get("flags", "")))
        elif fields.get("codec_type") == "audio":
            audio.append((pts if pts is not None else dts, dur if dur is not None else AAC_FRAME_SECONDS))
    return video, audio


# ------------------------------------------------------------ local checks

def check_media_playlist(report, name, rendition_dir):
    """Structural checks. Returns the parsed playlist, or None if unusable."""
    playlist = os.path.join(rendition_dir, "index.m3u8")
    if not os.path.isfile(playlist):
        report.error(f"{name}: index.m3u8 missing")
        return None
    info = common.parse_media_playlist(read_text(playlist))
    if not info["segments"]:
        report.error(f"{name}: playlist lists no segments")
        return None
    if (info["version"] or 0) < 6:
        report.error(f"{name}: EXT-X-VERSION {info['version']} is below 6 (required for EXT-X-MAP)")
    if not info["map_uri"]:
        report.error(f"{name}: no #EXT-X-MAP (fMP4 needs an initialization section)")
    if not info["endlist"]:
        report.error(f"{name}: no #EXT-X-ENDLIST (VOD playlist incomplete)")
    if info["discontinuities"]:
        report.error(f"{name}: {info['discontinuities']} #EXT-X-DISCONTINUITY tags")
    if info["has_byterange"]:
        report.warn(f"{name}: uses byte ranges (upload/CDN layout assumes one file per segment)")
    if not info["independent"]:
        report.warn(f"{name}: #EXT-X-INDEPENDENT-SEGMENTS missing in media playlist")
    longest = max(duration for duration, _ in info["segments"])
    if (info["target_duration"] or 0) < int(longest + 0.5):
        report.error(f"{name}: TARGETDURATION {info['target_duration']} < longest segment {longest:.3f}s")

    referenced = {uri for _, uri in info["segments"]}
    if info["map_uri"]:
        referenced.add(info["map_uri"])
    problems = 0
    for uri in sorted(referenced):
        path = os.path.join(rendition_dir, uri)
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            report.error(f"{name}: {uri} missing or empty")
            problems += 1
    for entry in sorted(os.listdir(rendition_dir)):
        if entry != "index.m3u8" and entry not in referenced:
            (report.error if entry.endswith(".m4s") else report.warn)(f"{name}: unreferenced file {entry}")
    if not problems:
        report.note(f"{name}: playlist OK ({len(info['segments'])} segments, init via EXT-X-MAP, ENDLIST present)")
    return info


def cumulative_boundaries(info):
    total, boundaries = 0.0, []
    for duration, _ in info["segments"][:-1]:
        total += duration
        boundaries.append(total)
    return boundaries


def check_keyframes(report, name, info, video, fps):
    keys = sorted(pts for pts, _, _, is_key in video if is_key and pts is not None)
    if not keys:
        report.error(f"{name}: no keyframes found")
        return
    frame = 1.0 / fps
    start = keys[0]
    bad = []
    for index, boundary in enumerate(cumulative_boundaries(info), start=1):
        target = start + boundary
        nearest = min(keys, key=lambda k: abs(k - target))
        if abs(nearest - target) > 1.5 * frame:
            bad.append((index, target, nearest))
    if bad:
        index, target, nearest = bad[0]
        report.error(f"{name}: {len(bad)} segment boundaries are not on a keyframe "
                     f"(first: segment {index} at {target:.3f}s, nearest keyframe {nearest:.3f}s)")
    else:
        report.note(f"{name}: all {len(info['segments'])} segments start on a keyframe")


def check_av(report, name, info, video, audio, fps):
    """Tolerant A/V checks: continuity, no drift, loose start offset. Returns the A/V start offset."""
    frame = 1.0 / fps
    dts = sorted(d for _, d, _, _ in video if d is not None)
    steps = [b - a for a, b in zip(dts, dts[1:])]
    video_bad = sum(1 for s in steps if s > 1.5 * frame or s < 0.5 * frame)
    if video_bad:
        report.error(f"{name}: {video_bad} irregular video timestamp steps (gaps or overlaps)")
    else:
        report.note(f"{name}: video timestamps continuous ({len(video)} frames)")

    if not audio:
        return None
    apts = sorted(p for p, _ in audio if p is not None)
    asteps = [b - a for a, b in zip(apts, apts[1:])]
    audio_bad = sum(1 for s in asteps if s > 1.5 * AAC_FRAME_SECONDS or s < 0.5 * AAC_FRAME_SECONDS)
    if audio_bad:
        report.error(f"{name}: {audio_bad} audio gaps/overlaps")
    else:
        report.note(f"{name}: audio timestamps continuous ({len(audio)} packets)")

    vpts = [p for p, _, _, _ in video if p is not None]
    video_span = max(vpts) + frame - min(vpts)
    audio_span = apts[-1] + AAC_FRAME_SECONDS - apts[0]
    drift = abs(audio_span - video_span)
    if drift > DRIFT_ERROR:
        report.error(f"{name}: audio and video lengths differ by {drift * 1000:.0f} ms")
    elif drift > DRIFT_WARN:
        report.warn(f"{name}: audio and video lengths differ by {drift * 1000:.0f} ms")
    else:
        report.note(f"{name}: no A/V drift ({drift * 1000:.0f} ms length difference)")

    keys = sorted(p for p, _, _, k in video if k and p is not None)
    edges = [keys[0]] + [keys[0] + b for b in cumulative_boundaries(info)] + [float("inf")]
    worst = 0.0
    for index, (duration, _) in enumerate(info["segments"]):
        low, high = edges[index] - 0.5 * frame, edges[index + 1] - 0.5 * frame
        total = sum(d for p, d in audio if p is not None and low <= p < high)
        worst = max(worst, abs(total - duration))
    if worst > SEGMENT_AV_ERROR:
        report.error(f"{name}: a segment's audio differs from its video by {worst * 1000:.0f} ms")
    elif worst > SEGMENT_AV_WARN:
        report.warn(f"{name}: a segment's audio differs from its video by {worst * 1000:.0f} ms")

    offset = apts[0] - min(vpts)
    if abs(offset) > START_OFFSET_WARN:
        report.warn(f"{name}: audio starts {offset * 1000:+.0f} ms from video")
    return offset


def run_decode_checks(report, name, playlist, duration):
    try:
        decode = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", playlist, "-f", "null", "-"],
                                capture_output=True, text=True, timeout=DECODE_TIMEOUT)
    except subprocess.TimeoutExpired:
        report.error(f"{name}: full decode timed out after {DECODE_TIMEOUT}s")
        return
    if decode.returncode != 0 or decode.stderr.strip():
        first = decode.stderr.strip().splitlines()[:3]
        report.error(f"{name}: decode errors: {' | '.join(first) or 'ffmpeg failed'}")
        return
    rng = random.Random(1234)
    failures = 0
    for _ in range(SEEK_SAMPLES):
        t = rng.uniform(0, max(0.0, duration - 2))
        try:
            seek = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{t:.3f}", "-i", playlist,
                                   "-frames:v", "8", "-f", "null", "-"],
                                  capture_output=True, text=True, timeout=SEEK_TIMEOUT)
        except subprocess.TimeoutExpired:
            failures += 1
            continue
        if seek.returncode != 0 or seek.stderr.strip():
            failures += 1
    if failures:
        report.error(f"{name}: {failures}/{SEEK_SAMPLES} seek tests failed")
    else:
        report.note(f"{name}: full decode clean, {SEEK_SAMPLES} random seeks OK")


def validate_local(version_dir, expected_names=None, expect_audio=None, deep_decode=True):
    report = Report()
    master_path = os.path.join(version_dir, "master.m3u8")
    if not os.path.isfile(master_path):
        report.error("master.m3u8 missing")
        return report
    master = common.parse_master_playlist(read_text(master_path))
    variants = master["variants"]
    if not variants:
        report.error("master.m3u8 lists no variants")
        return report
    if not master["independent"]:
        report.error("master.m3u8 lacks #EXT-X-INDEPENDENT-SEGMENTS")

    names = [v["URI"].split("/")[0] for v in variants]
    if expected_names is not None and sorted(names) != sorted(expected_names):
        report.error(f"master variants {sorted(names)} differ from expected {sorted(expected_names)}")
    report.note(f"master lists {len(variants)} variant(s): {', '.join(names)}")

    parsed, offsets, boundary_sets = {}, {}, {}
    for variant, name in zip(variants, names):
        rendition_dir = os.path.join(version_dir, name)
        playlist = os.path.join(rendition_dir, "index.m3u8")
        if not os.path.isfile(playlist):
            report.error(f"{name}: variant URI {variant['URI']} does not resolve")
            continue
        if variant.get("CLOSED-CAPTIONS") != "NONE":
            report.error(f"{name}: master variant lacks CLOSED-CAPTIONS=NONE (Apple requires it when the "
                         "stream has no caption or subtitle renditions)")
        errors_before = len(report.errors)
        info = check_media_playlist(report, name, rendition_dir)
        if info is None or len(report.errors) > errors_before:
            # Structurally broken (missing files, no ENDLIST...): deeper probing would crash or, for a
            # playlist without ENDLIST, make ffprobe wait forever for a "live" update.
            report.note(f"{name}: skipped deep checks because the playlist is structurally invalid")
            continue
        parsed[name] = info
        boundary_sets[name] = cumulative_boundaries(info)

        # master accuracy: recompute with the same RFC 8216 rules and compare
        durations = [d for d, _ in info["segments"]]
        sizes = [os.path.getsize(os.path.join(rendition_dir, uri)) for _, uri in info["segments"]]
        peak, average = common.segment_bit_rates(sizes, durations, info["target_duration"])
        for key, value in (("BANDWIDTH", peak), ("AVERAGE-BANDWIDTH", average)):
            declared = _num(variant.get(key))
            if declared is None or abs(declared - value) > BANDWIDTH_TOLERANCE * value + 1:
                report.error(f"{name}: master {key}={variant.get(key)} but files imply {value:.0f}")
        with open(os.path.join(rendition_dir, info["map_uri"]), "rb") as handle:
            actual_video = common.avc_codec_string(handle.read())
        declared_codecs = variant.get("CODECS", "")
        if actual_video not in declared_codecs:
            report.error(f"{name}: master CODECS {declared_codecs!r} does not match init avcC {actual_video}")
        declared_audio = common.AAC_LC_CODEC in declared_codecs

        try:
            video, audio = probe_packets(playlist)
        except RuntimeError as err:
            report.error(f"{name}: cannot read stream through playlist: {err}")
            continue
        if not video:
            report.error(f"{name}: no video packets decoded from playlist")
            continue
        if audio and not declared_audio:
            report.error(f"{name}: has audio but master CODECS omits {common.AAC_LC_CODEC}")
        if not audio and declared_audio:
            report.error(f"{name}: master CODECS claims audio but the stream has none")
        if expect_audio is not None and bool(audio) != expect_audio:
            report.error(f"{name}: audio presence ({bool(audio)}) differs from the source ({expect_audio})")

        declared_fps = _num(variant.get("FRAME-RATE")) or 30.0
        dts = sorted(d for _, d, _, _ in video if d is not None)
        measured_fps = (len(dts) - 1) / (dts[-1] - dts[0]) if len(dts) > 1 and dts[-1] > dts[0] else declared_fps
        if abs(measured_fps - declared_fps) > 0.02 * declared_fps:
            report.error(f"{name}: master FRAME-RATE {declared_fps} but stream measures {measured_fps:.3f}")
        try:
            size = common.ffprobe_json(os.path.join(rendition_dir, info["map_uri"]), "-select_streams", "v:0",
                                       "-show_entries", "stream=width,height")["streams"][0]
            if f"{size['width']}x{size['height']}" != variant.get("RESOLUTION"):
                report.error(f"{name}: master RESOLUTION {variant.get('RESOLUTION')} but stream is "
                             f"{size['width']}x{size['height']}")
        except (RuntimeError, KeyError, IndexError):
            report.error(f"{name}: could not read stream dimensions from init segment")

        check_keyframes(report, name, info, video, declared_fps)
        offsets[name] = check_av(report, name, info, video, audio, declared_fps)
        if deep_decode:
            run_decode_checks(report, name, playlist, sum(durations))

    # cross-rendition consistency
    names_ok = [n for n in names if n in boundary_sets]
    if len(names_ok) > 1:
        ref = names_ok[0]
        fps_ref = _num(variants[names.index(ref)].get("FRAME-RATE")) or 30.0
        for other in names_ok[1:]:
            a, b = boundary_sets[ref], boundary_sets[other]
            if len(a) != len(b):
                report.error(f"segment count differs: {ref} has {len(a) + 1}, {other} has {len(b) + 1}")
            elif any(abs(x - y) > 1.0 / fps_ref for x, y in zip(a, b)):
                report.error(f"segment boundaries differ between {ref} and {other}")
            else:
                report.note(f"{other}: segment boundaries match {ref}")
        known = [(n, o) for n, o in offsets.items() if o is not None]
        for n, o in known[1:]:
            if abs(o - known[0][1]) > 1.0 / fps_ref:
                report.error(f"A/V start offset differs across renditions ({known[0][0]} "
                             f"{known[0][1] * 1000:+.0f} ms vs {n} {o * 1000:+.0f} ms): switching would shift sync")
    return report


# ----------------------------------------------------------- remote checks

def http_request(url, method="GET", timeout=30):
    request = urllib.request.Request(url, method=method, headers={"User-Agent": "hls-validator/1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read() if method == "GET" else b""
        return response.status, dict(response.headers), body


def http_retry(url, method="GET", attempts=3):
    last = None
    for _ in range(attempts):
        try:
            return http_request(url, method)
        except (urllib.error.URLError, OSError) as err:
            last = err
    raise RuntimeError(f"{method} {url} failed: {last}")


def validate_remote(master_url, local_dir=None, expected_names=None, workers=16):
    """Playlist/structure checks over HTTP plus a decode sample. Deep packet analysis stays local."""
    report = Report()
    base = master_url.rsplit("/", 1)[0] + "/"
    try:
        status, headers, body = http_retry(master_url)
    except RuntimeError as err:
        report.error(str(err))
        return report
    master = common.parse_master_playlist(body.decode("utf-8", "replace"))
    if not master["variants"]:
        report.error("remote master.m3u8 lists no variants")
        return report
    names = [v["URI"].split("/")[0] for v in master["variants"]]
    if expected_names is not None and sorted(names) != sorted(expected_names):
        report.error(f"remote variants {sorted(names)} differ from expected {sorted(expected_names)}")
    report.note(f"remote master reachable, {len(names)} variant(s)")
    content_type = {k.lower(): v for k, v in headers.items()}.get("content-type", "")
    if "mpegurl" not in content_type.lower():
        report.warn(f"master.m3u8 Content-Type is {content_type!r}")

    files = []   # (relative path, expected local size or None)
    durations = {}
    for variant, name in zip(master["variants"], names):
        try:
            _, _, playlist_body = http_retry(base + variant["URI"])
        except RuntimeError as err:
            report.error(str(err))
            continue
        info = common.parse_media_playlist(playlist_body.decode("utf-8", "replace"))
        if not (info["endlist"] and info["map_uri"] and info["segments"]):
            report.error(f"{name}: remote playlist incomplete (ENDLIST/MAP/segments)")
            continue
        durations[name] = sum(d for d, _ in info["segments"])
        for uri in [info["map_uri"]] + [u for _, u in info["segments"]]:
            rel = f"{name}/{uri}"
            size = None
            if local_dir and os.path.isfile(os.path.join(local_dir, rel)):
                size = os.path.getsize(os.path.join(local_dir, rel))
            files.append((rel, size))

    def check(item):
        rel, expected = item
        try:
            status, headers, _ = http_retry(base + rel, "HEAD")
        except RuntimeError as err:
            return f"{rel}: {err}"
        length = headers.get("Content-Length")
        if status != 200:
            return f"{rel}: HTTP {status}"
        if expected is not None and length is not None and int(length) != expected:
            return f"{rel}: size {length} != local {expected}"
        return None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        problems = [p for p in pool.map(check, files) if p]
    if problems:
        report.error(f"{len(problems)}/{len(files)} remote files failed (first: {problems[0]})")
    else:
        report.note(f"all {len(files)} remote files reachable" + (" with matching sizes" if local_dir else ""))

    for name in names:
        if name not in durations:
            continue
        url = f"{base}{name}/index.m3u8"
        failures = 0
        total = durations[name]
        # read_intervals syntax is START%END: here START seconds, then the next 15 packets.
        for position in (0.0, total / 2, max(0.0, total - 3)):
            cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-read_intervals", f"{position:.3f}%+#15",
                   "-show_entries", "packet=pts_time", "-of", "csv=p=0", url]
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=SEEK_TIMEOUT)
            except subprocess.TimeoutExpired:
                failures += 1
                continue
            if result.returncode != 0 or len(result.stdout.split()) < 5:
                failures += 1
        (report.error if failures else report.note)(
            f"{name}: remote read at start/middle/end " + (f"failed {failures}/3" if failures else "OK"))
    return report


# --------------------------------------------------- Apple's validator (opt)

def apple_validate(target):
    tool = shutil.which("mediastreamvalidator")
    if not tool:
        print("mediastreamvalidator not found (download 'HTTP Live Streaming Tools' from the Apple "
              "Developer site). Skipping.")
        return
    server = None
    url = target
    if not target.startswith("http"):
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=target)
        handler.log_message = lambda *a, **k: None
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_address[1]}/master.m3u8"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            # The default 300 s runtime cap stops after ~20% of an 18-minute video's segments, and the
            # peak-bitrate comparison is then made against partial data. Lift it so the whole stream is checked.
            result = subprocess.run([tool, "--timeout", "3600", "-O", os.path.join(tmp, "report.json"), url],
                                    capture_output=True, text=True)
            print(result.stdout or result.stderr)
    finally:
        if server:
            server.shutdown()


def main():
    parser = argparse.ArgumentParser(description="Validate an HLS fMP4 version (folder or URL).")
    parser.add_argument("target", help="Version folder, or the https URL of master.m3u8")
    parser.add_argument("--local", help="With a URL: local folder to compare file sizes against")
    parser.add_argument("--no-decode", action="store_true", help="Skip the full decode and seek tests (local)")
    parser.add_argument("--apple-validate", action="store_true", help="Also run mediastreamvalidator if installed")
    args = parser.parse_args()
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit(f"Error: {tool} not found")

    if args.target.startswith("http"):
        report = validate_remote(args.target, local_dir=args.local)
    else:
        report = validate_local(args.target, deep_decode=not args.no_decode)
    report.print()
    if args.apple_validate:
        apple_validate(args.target)
    sys.exit(0 if report.ok else 1)


if __name__ == "__main__":
    main()
