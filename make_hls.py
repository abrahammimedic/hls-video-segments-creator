#!/usr/bin/env python3
"""Encode videos into adaptive HLS (fMP4/CMAF, 6 s segments) and optionally upload to Cloudflare R2.

Per video this produces immutable, versioned output:

    <out>/<slug>/v<N>/master.m3u8
    <out>/<slug>/v<N>/<360p|720p|1080p>/{index.m3u8, init.mp4, seg_00000.m4s, ...}

Work files (partial encodes, fingerprints, receipts) live in <out>/<slug>/.work/ and are never uploaded.
Renditions never exceed the source resolution, the source's aspect ratio and orientation are kept
(portrait stays portrait), and every rendition shares one frame rate and a fixed 2 s GOP so that
6 s segment boundaries line up across renditions.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction

import hls_common as common
from hls_common import GOP_SECONDS, SEGMENT_SECONDS, human_size

TOOL_VERSION = "1"

# Chosen from bitrate_trial.py on the exercise footage (2026-10-15): CRF 20 settles near 1.5 Mbps at 720p and
# 3.5-4 Mbps at 1080p, and these rates lift the worst-frame VMAF by ~2-3 points over 1100 / 2500.
LADDER = [
    {"name": "360p", "short": 360, "kbps": 400},
    {"name": "720p", "short": 720, "kbps": 1500},
    {"name": "1080p", "short": 1080, "kbps": 3500},
]
LADDER_TOLERANCE = 0.97          # a step is offered if the source short side is >= step * 0.97
NATIVE_REFERENCE_PIXELS = 640 * 360
NATIVE_MAX_KBPS = LADDER[0]["kbps"]
NATIVE_MIN_KBPS = 150

MAXRATE_MULTIPLIER = 1.5
BUFSIZE_MULTIPLIER = 2
AUDIO_ARGS = ["-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2"]
MAX_FPS = 30.5

STANDARD_RATES = [Fraction(24000, 1001), Fraction(24), Fraction(25), Fraction(30000, 1001), Fraction(30)]

# (level, max frame size in macroblocks, max macroblocks/second) from the H.264 spec.
H264_LEVELS = [
    ("3.0", 1620, 40500), ("3.1", 3600, 108000), ("3.2", 5120, 216000),
    ("4.0", 8192, 245760), ("4.2", 8704, 522240), ("5.1", 36864, 983040),
]


@dataclass
class SourceInfo:
    width: int          # display width/height: after rotation and pixel-aspect correction
    height: int
    fps: Fraction
    duration: float
    has_audio: bool


@dataclass
class Rendition:
    name: str
    width: int
    height: int
    kbps: int


# ------------------------------------------------------------------ probing

def probe_source(path):
    data = common.ffprobe_json(path, "-show_streams", "-show_format")
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    if video is None:
        raise ValueError(f"no video stream found in {path}")

    width, height = int(video["width"]), int(video["height"])
    sar = common.parse_fraction(str(video.get("sample_aspect_ratio", "")).replace(":", "/"))
    if sar and sar != 1:
        width = round(width * float(sar))

    rotation = None
    for side in video.get("side_data_list", []):
        if "rotation" in side:
            rotation = side["rotation"]
    if rotation is None and "rotate" in video.get("tags", {}):
        rotation = video["tags"]["rotate"]
    if rotation is not None and int(round(float(rotation))) % 180 != 0:
        width, height = height, width

    fps = (common.parse_fraction(video.get("avg_frame_rate"))
           or common.parse_fraction(video.get("r_frame_rate")) or Fraction(30))
    duration = float(data.get("format", {}).get("duration") or video.get("duration") or 0)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    return SourceInfo(width, height, fps, duration, has_audio)


# ------------------------------------------------------ pure planning logic

def choose_fps(source_fps):
    """Constant output frame rate: snap near-standard rates, halve anything above ~30."""
    fps = Fraction(source_fps)
    while fps > Fraction(MAX_FPS).limit_denominator(100):
        fps /= 2
    # Nearest standard rate wins: 24 and 23.976 (30 and 29.97) are only 0.1% apart, so "first within
    # tolerance" would retime an exact 24/30 fps source to NTSC. The 3% window also absorbs the
    # slightly-off average rates reported by variable-frame-rate phone recordings.
    nearest = min(STANDARD_RATES, key=lambda s: abs(float(fps) - float(s)))
    if abs(float(fps) - float(nearest)) / float(nearest) <= 0.03:
        return nearest
    return Fraction(max(1, round(float(fps))))


def gop_frames(fps):
    return max(1, round(float(fps) * GOP_SECONDS))


def even(value):
    return max(2, 2 * round(value / 2))


def scaled_dims(display_w, display_h, target_short):
    """Dimensions with the given short side, aspect ratio and orientation preserved, both even."""
    if display_w >= display_h:
        return even(display_w * target_short / display_h), even(target_short)
    return even(target_short), even(display_h * target_short / display_w)


def select_renditions(display_w, display_h, only=None):
    """Pick ladder steps the source can support. Never upscales.

    A step is offered when the source short side is within 3% of it (the rendition then uses the
    source's own size). A source smaller than the lowest step gets one native-resolution rendition.
    """
    short = min(display_w, display_h)
    steps = [s for s in LADDER if short >= s["short"] * LADDER_TOLERANCE]
    renditions = []
    if steps:
        for step in steps:
            width, height = scaled_dims(display_w, display_h, min(step["short"], short))
            renditions.append(Rendition(step["name"], width, height, step["kbps"]))
    else:
        width, height = scaled_dims(display_w, display_h, short)
        scaled = int(NATIVE_MAX_KBPS * width * height / NATIVE_REFERENCE_PIXELS)
        kbps = max(NATIVE_MIN_KBPS, min(NATIVE_MAX_KBPS, scaled))
        renditions.append(Rendition(f"{min(width, height)}p", width, height, kbps))

    if only:
        wanted = {name.strip() for name in only.split(",") if name.strip()}
        available = [r.name for r in renditions]
        picked = [r for r in renditions if r.name in wanted]
        if not picked:
            raise ValueError(f"--only {only!r} matches nothing this source supports (available: {available})")
        renditions = picked
    return renditions


def pick_level(width, height, fps):
    frame_mbs = ((width + 15) // 16) * ((height + 15) // 16)
    mb_rate = frame_mbs * float(fps)
    for level, max_frame_mbs, max_mb_rate in H264_LEVELS:
        if frame_mbs <= max_frame_mbs and mb_rate <= max_mb_rate:
            return level
    return H264_LEVELS[-1][0]


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "video"


def local_versions(slug_dir):
    if not os.path.isdir(slug_dir):
        return []
    found = []
    for entry in os.listdir(slug_dir):
        match = re.fullmatch(r"v(\d+)", entry)
        if match and os.path.isdir(os.path.join(slug_dir, entry)):
            found.append(int(match.group(1)))
    return sorted(found)


def choose_version(slug_dir, requested=None, remote=()):
    """Explicit version wins; otherwise resume an unfinished local version, else take the next free one."""
    if requested:
        return requested
    local = local_versions(slug_dir)
    if local:
        latest = local[-1]
        unfinished = not os.path.exists(os.path.join(slug_dir, f"v{latest}", "master.m3u8"))
        if unfinished and latest not in set(remote):
            return latest
    return max(local + list(remote) + [0]) + 1


# --------------------------------------------------------- ffmpeg commands

def video_filter(r, fps):
    return f"fps={fps.numerator}/{fps.denominator},scale={r.width}:{r.height}:flags=lanczos,setsar=1"


def video_args(r, fps, preset, pass_number=None, passlog=None):
    """x264 settings shared by production encodes and the bitrate trial."""
    gop = str(gop_frames(fps))
    args = [
        "-c:v", "libx264", "-profile:v", "main", "-level:v", pick_level(r.width, r.height, fps),
        "-pix_fmt", "yuv420p",
        "-b:v", f"{r.kbps}k",
        "-maxrate", f"{int(r.kbps * MAXRATE_MULTIPLIER)}k",
        "-bufsize", f"{int(r.kbps * BUFSIZE_MULTIPLIER)}k",
        "-g", gop, "-keyint_min", gop, "-sc_threshold", "0",
        "-preset", preset,
    ]
    if pass_number:
        args += ["-pass", str(pass_number), "-passlogfile", passlog]
    return args


def ffmpeg_prefix(input_path):
    return ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-stats", "-y", "-i", input_path]


def pass1_command(input_path, r, fps, preset, passlog):
    return (ffmpeg_prefix(input_path)
            + ["-map", "0:v:0", "-vf", video_filter(r, fps)]
            + video_args(r, fps, preset, 1, passlog)
            + ["-an", "-f", "null", os.devnull])


def pass2_command(input_path, r, fps, preset, passlog, out_dir, has_audio):
    cmd = (ffmpeg_prefix(input_path)
           + ["-map", "0:v:0"] + (["-map", "0:a:0?"] if has_audio else [])
           + ["-map_metadata", "-1", "-map_chapters", "-1", "-vf", video_filter(r, fps)]
           + video_args(r, fps, preset, 2, passlog))
    if has_audio:
        cmd += AUDIO_ARGS
    cmd += [
        "-f", "hls", "-hls_time", str(SEGMENT_SECONDS), "-hls_playlist_type", "vod",
        "-hls_segment_type", "fmp4", "-hls_fmp4_init_filename", "init.mp4",
        "-hls_flags", "independent_segments",
        "-hls_segment_filename", os.path.join(out_dir, "seg_%05d.m4s"),
        os.path.join(out_dir, "index.m3u8"),
    ]
    return cmd


# --------------------------------------------------- completeness / resume

def ffmpeg_version():
    result = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True)
    return (result.stdout.splitlines() or ["unknown"])[0]


def fingerprint(input_path, r, fps, preset, has_audio):
    stat = os.stat(input_path)
    return {
        "tool_version": TOOL_VERSION,
        "input": os.path.abspath(input_path), "input_size": stat.st_size, "input_mtime_ns": stat.st_mtime_ns,
        "width": r.width, "height": r.height, "kbps": r.kbps,
        "fps": f"{fps.numerator}/{fps.denominator}", "gop": gop_frames(fps),
        "preset": preset, "segment_seconds": SEGMENT_SECONDS, "audio": has_audio,
        "audio_args": AUDIO_ARGS if has_audio else None,
        "maxrate_x": MAXRATE_MULTIPLIER, "bufsize_x": BUFSIZE_MULTIPLIER,
        "ffmpeg": ffmpeg_version(),
    }


def rendition_is_complete(rendition_dir, source_duration):
    """True only for a fully written rendition: ENDLIST, every file present and non-empty, sane duration."""
    playlist = os.path.join(rendition_dir, "index.m3u8")
    if not os.path.isfile(playlist):
        return False
    with open(playlist, encoding="utf-8") as handle:
        info = common.parse_media_playlist(handle.read())
    if not (info["endlist"] and info["segments"] and info["map_uri"]):
        return False
    needed = [info["map_uri"]] + [uri for _, uri in info["segments"]]
    for name in needed:
        path = os.path.join(rendition_dir, name)
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            return False
    total = sum(duration for duration, _ in info["segments"])
    return abs(total - source_duration) <= max(2.0, 0.02 * source_duration)


def read_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def encode_rendition(input_path, info, r, fps, preset, version_dir, work_dir, force):
    """Encode one rendition into version_dir/<name>. Returns 'skipped' or 'encoded'."""
    final_dir = os.path.join(version_dir, r.name)
    print_path = os.path.join(work_dir, f"{r.name}.encode.json")
    expected = fingerprint(input_path, r, fps, preset, info.has_audio)

    if not force and read_json(print_path) == expected and rendition_is_complete(final_dir, info.duration):
        print(f"  {r.name}: already complete with identical settings, skipping")
        return "skipped"

    partial = os.path.join(work_dir, f"{r.name}.partial")
    shutil.rmtree(partial, ignore_errors=True)
    os.makedirs(partial)

    with tempfile.TemporaryDirectory(dir=work_dir, prefix="passlog_") as log_dir:
        passlog = os.path.join(log_dir, "ffmpeg2pass")
        started = time.time()
        print(f"  {r.name} ({r.width}x{r.height}, {r.kbps} kbps): pass 1/2 (analysis)")
        if subprocess.run(pass1_command(input_path, r, fps, preset, passlog)).returncode != 0:
            raise RuntimeError(f"{r.name}: ffmpeg pass 1 failed")
        print(f"  {r.name}: pass 1 done in {time.time() - started:.0f}s, pass 2/2 (encoding)")
        mid = time.time()
        if subprocess.run(pass2_command(input_path, r, fps, preset, passlog, partial,
                                        info.has_audio)).returncode != 0:
            raise RuntimeError(f"{r.name}: ffmpeg pass 2 failed")
        print(f"  {r.name}: pass 2 done in {time.time() - mid:.0f}s")

    if not rendition_is_complete(partial, info.duration):
        raise RuntimeError(f"{r.name}: encode finished but output is incomplete")

    os.makedirs(version_dir, exist_ok=True)
    shutil.rmtree(final_dir, ignore_errors=True)
    os.replace(partial, final_dir)
    with open(print_path, "w", encoding="utf-8") as handle:
        json.dump(expected, handle, indent=2)
    return "encoded"


# --------------------------------------------------------- master playlist

def rendition_stats(rendition_dir):
    """Read a finished rendition: durations, sizes, RFC 8216 bit rates, exact codec string."""
    with open(os.path.join(rendition_dir, "index.m3u8"), encoding="utf-8") as handle:
        info = common.parse_media_playlist(handle.read())
    durations = [d for d, _ in info["segments"]]
    sizes = [os.path.getsize(os.path.join(rendition_dir, uri)) for _, uri in info["segments"]]
    peak, average = common.segment_bit_rates(sizes, durations, info["target_duration"])
    with open(os.path.join(rendition_dir, info["map_uri"]), "rb") as handle:
        video_codec = common.avc_codec_string(handle.read())
    return info, peak, average, video_codec


def probe_dims(init_path):
    data = common.ffprobe_json(init_path, "-select_streams", "v:0", "-show_entries", "stream=width,height")
    stream = data["streams"][0]
    return int(stream["width"]), int(stream["height"])


def build_master(version_dir, renditions, fps, has_audio):
    lines = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-INDEPENDENT-SEGMENTS"]
    variants = []
    for r in sorted(renditions, key=lambda item: item.kbps):
        rendition_dir = os.path.join(version_dir, r.name)
        _, peak, average, video_codec = rendition_stats(rendition_dir)
        width, height = probe_dims(os.path.join(rendition_dir, "init.mp4"))
        codecs = f"{video_codec},{common.AAC_LC_CODEC}" if has_audio else video_codec
        lines.append(
            f'#EXT-X-STREAM-INF:BANDWIDTH={int(round(peak))},AVERAGE-BANDWIDTH={int(round(average))},'
            f'CODECS="{codecs}",RESOLUTION={width}x{height},FRAME-RATE={common.format_frame_rate(fps)},'
            "CLOSED-CAPTIONS=NONE")   # Apple: MUST be declared when there are no caption/subtitle renditions
        lines.append(f"{r.name}/index.m3u8")
        variants.append({"name": r.name, "bandwidth": int(round(peak)), "average_bandwidth": int(round(average)),
                         "codecs": codecs, "resolution": f"{width}x{height}"})
    path = os.path.join(version_dir, "master.m3u8")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    return variants


def version_file_stats(version_dir):
    count, total = 0, 0
    for root, _, files in os.walk(version_dir):
        for name in files:
            count += 1
            total += os.path.getsize(os.path.join(root, name))
    return count, total


# ---------------------------------------------------------------- pipeline

def build_hls(input_path, args, remote_ctx=None):
    """Full pipeline for one input. Returns (version_dir, slug, version) on success."""
    info = probe_source(input_path)
    fps = choose_fps(info.fps)
    renditions = select_renditions(info.width, info.height, args.only)
    slug = args.name or slugify(os.path.splitext(os.path.basename(input_path))[0])
    out_dir = args.out_dir or os.path.join(os.path.dirname(os.path.abspath(input_path)), "hls_output")
    slug_dir = os.path.join(out_dir, slug)

    remote = remote_ctx["versions"](args.remote_path) if remote_ctx else []
    version = choose_version(slug_dir, args.version, remote)
    version_dir = os.path.join(slug_dir, f"v{version}")
    work_dir = os.path.join(slug_dir, ".work", f"v{version}")

    if os.path.exists(os.path.join(version_dir, "master.m3u8")) and not args.force:
        raise RuntimeError(f"{version_dir} is already built. Versions are immutable: omit --version for "
                           "the next one, or pass --force to rebuild it locally")
    if version in set(remote):
        raise RuntimeError(f"version v{version} already exists in R2 under '{args.remote_path}'; pick a new --version")
    if args.force and os.path.exists(os.path.join(version_dir, "master.m3u8")):
        os.remove(os.path.join(version_dir, "master.m3u8"))
    os.makedirs(work_dir, exist_ok=True)

    print(f"\n== {os.path.basename(input_path)}: {info.width}x{info.height} @ {float(info.fps):.3f} fps, "
          f"{info.duration:.0f}s, {'with' if info.has_audio else 'no'} audio")
    print(f"   -> {slug} v{version}: " + ", ".join(f"{r.name} ({r.width}x{r.height})" for r in renditions)
          + f" at {common.format_frame_rate(fps)} fps, {SEGMENT_SECONDS}s segments")
    if not info.has_audio:
        print("   (source has no audio track; renditions will be video-only)")

    for r in renditions:
        encode_rendition(input_path, info, r, fps, args.preset, version_dir, work_dir, args.force)

    variants = build_master(version_dir, renditions, fps, info.has_audio)

    import validate_hls
    report = validate_hls.validate_local(version_dir, expected_names=[r.name for r in renditions],
                                         expect_audio=info.has_audio)
    report.print()
    if not report.ok:
        os.remove(os.path.join(version_dir, "master.m3u8"))
        raise RuntimeError("validation failed; master.m3u8 removed so this version is not treated as built. "
                           "Fix the cause and re-run (completed renditions are reused)")

    file_count, total_bytes = version_file_stats(version_dir)
    receipt = {"slug": slug, "version": version, "source": os.path.abspath(input_path),
               "created": datetime.now(timezone.utc).isoformat(), "files": file_count, "bytes": total_bytes,
               "fps": common.format_frame_rate(fps), "variants": variants}
    with open(os.path.join(work_dir, "publish.json"), "w", encoding="utf-8") as handle:
        json.dump(receipt, handle, indent=2)
    print(f"\n   Built {version_dir}\n   {file_count} files, {human_size(total_bytes)}")
    return version_dir, slug, version


def parse_args():
    parser = argparse.ArgumentParser(
        description="Encode videos to adaptive HLS (fMP4, 6 s segments) and optionally upload to R2.")
    parser.add_argument("inputs", nargs="+", help="One or more input video files")
    parser.add_argument("-d", "--out-dir", help="Output root (default: <input folder>/hls_output)")
    parser.add_argument("--name", help="Slug used in paths/URLs (default: derived from the file name)")
    parser.add_argument("--version", type=int, help="Version number (default: next unused, or resume an unfinished one)")
    parser.add_argument("--only", help="Comma-separated renditions to build, e.g. 360p,720p")
    parser.add_argument("--preset", default="slow", help="x264 preset (default: slow)")
    parser.add_argument("--force", action="store_true", help="Re-encode even if renditions look complete")
    parser.add_argument("--upload", action="store_true", help="Upload to R2 after a successful validation")
    parser.add_argument("--remote-path", help="With --upload: bucket folder to publish into, e.g. "
                        "exercises/trimester_2/trimester_2_yoga (the version folder vN is added automatically)")
    parser.add_argument("--dry-run", action="store_true",
                        help="With --upload: show what would be uploaded without touching R2")
    args = parser.parse_args()
    if len(args.inputs) > 1 and (args.name or args.version or args.remote_path):
        parser.error("--name, --version and --remote-path only make sense with a single input")
    if args.upload:
        if not args.remote_path:
            parser.error("--upload needs --remote-path (the bucket folder, e.g. exercises/trimester_2/my_video)")
        import r2_upload
        try:
            args.remote_path = r2_upload.normalize_base_path(args.remote_path)
        except ValueError as err:
            parser.error(str(err))
    return args


def main():
    args = parse_args()
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit(f"Error: {tool} not found. Install it with: brew install ffmpeg")

    remote_ctx = None
    client = settings = None
    if args.upload:
        # Fail fast on missing credentials/boto3, before hours of encoding.
        import r2_upload
        settings = r2_upload.load_settings()
        client = r2_upload.make_client(settings)
        remote_ctx = {"versions": lambda base: r2_upload.remote_versions(client, settings["bucket"], base)}

    failures = []
    for input_path in args.inputs:
        if not os.path.isfile(input_path):
            print(f"Skipping, file not found: {input_path}")
            failures.append(input_path)
            continue
        try:
            version_dir, slug, version = build_hls(input_path, args, remote_ctx)
            if args.upload:
                r2_upload.upload_version(client, settings, version_dir, args.remote_path, version,
                                         dry_run=args.dry_run, skip_validate=True)  # build_hls just validated
        except (RuntimeError, ValueError) as err:
            print(f"\nFAILED {input_path}: {err}")
            failures.append(input_path)

    if failures:
        print("\nFailed:")
        for item in failures:
            print(f"  - {item}")
        sys.exit(1)


if __name__ == "__main__":
    main()
