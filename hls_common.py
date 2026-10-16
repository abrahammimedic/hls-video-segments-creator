"""Shared, mostly pure helpers for the HLS tools (parsing, RFC 8216 bandwidth math, codec strings).

Kept free of encoding/upload side effects so make_hls.py, validate_hls.py and r2_upload.py
(and the unit tests) can all import it.
"""

import json
import re
import subprocess
from fractions import Fraction

SEGMENT_SECONDS = 6
GOP_SECONDS = 2
EPS = 1e-6


def human_size(num_bytes):
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"


def parse_fraction(text):
    """'30000/1001' -> Fraction; returns None for '0/0', 'N/A' or garbage."""
    try:
        value = Fraction(str(text))
    except (ValueError, ZeroDivisionError):
        return None
    return value if value > 0 else None


def ffprobe_json(path, *extra_args):
    """Run ffprobe with JSON output and return the parsed document."""
    cmd = ["ffprobe", "-v", "error", "-of", "json", *extra_args, path]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"ffprobe timed out for {path}")
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}: {result.stderr.strip()}")
    return json.loads(result.stdout or "{}")


# ---------------------------------------------------------------- playlists

_ATTR_RE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


def parse_attributes(text):
    """Parse an HLS attribute list ('A=1,B="x,y"') into a dict (quotes stripped)."""
    return {key: value.strip('"') for key, value in _ATTR_RE.findall(text)}


def parse_media_playlist(text):
    info = {
        "version": None, "target_duration": None, "map_uri": None, "endlist": False,
        "discontinuities": 0, "playlist_type": None, "independent": False,
        "has_byterange": False, "segments": [],   # segments: [(duration, uri)]
    }
    pending = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            pending = float(line[len("#EXTINF:"):].split(",")[0])
        elif line.startswith("#EXT-X-VERSION:"):
            info["version"] = int(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-TARGETDURATION:"):
            info["target_duration"] = int(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-MAP:"):
            info["map_uri"] = parse_attributes(line[len("#EXT-X-MAP:"):]).get("URI")
        elif line.startswith("#EXT-X-PLAYLIST-TYPE:"):
            info["playlist_type"] = line.split(":", 1)[1]
        elif line == "#EXT-X-DISCONTINUITY":
            info["discontinuities"] += 1
        elif line == "#EXT-X-ENDLIST":
            info["endlist"] = True
        elif line == "#EXT-X-INDEPENDENT-SEGMENTS":
            info["independent"] = True
        elif line.startswith("#EXT-X-BYTERANGE"):
            info["has_byterange"] = True
        elif not line.startswith("#") and pending is not None:
            info["segments"].append((pending, line))
            pending = None
    return info


def parse_master_playlist(text):
    """Return {'independent': bool, 'variants': [attrs + 'URI']} in file order."""
    result = {"independent": False, "variants": []}
    pending = None
    for raw in text.splitlines():
        line = raw.strip()
        if line == "#EXT-X-INDEPENDENT-SEGMENTS":
            result["independent"] = True
        elif line.startswith("#EXT-X-STREAM-INF:"):
            pending = parse_attributes(line[len("#EXT-X-STREAM-INF:"):])
        elif line and not line.startswith("#") and pending is not None:
            pending["URI"] = line
            result["variants"].append(pending)
            pending = None
    return result


# ------------------------------------------------- RFC 8216 bandwidth math

def segment_bit_rates(sizes_bytes, durations, target_duration):
    """Return (peak_bps, average_bps) per RFC 8216 sections 4.1 and 4.3.4.2.

    segment bit rate = size / EXTINF duration (container overhead included).
    peak    = largest bit rate of any contiguous run of segments whose total duration
              is between 0.5x and 1.5x the target duration.
    average = total bits / total playlist duration.
    If no run qualifies (pathologically short playlists) the average is used for both.
    """
    if not sizes_bytes or len(sizes_bytes) != len(durations):
        raise ValueError("sizes and durations must be equal-length, non-empty lists")
    average = 8 * sum(sizes_bytes) / sum(durations)
    low, high = 0.5 * target_duration, 1.5 * target_duration
    peak = None
    count = len(sizes_bytes)
    for start in range(count):
        bits = 0.0
        span = 0.0
        for end in range(start, count):
            bits += 8 * sizes_bytes[end]
            span += durations[end]
            if span > high + EPS:
                break
            if span >= low - EPS:
                rate = bits / span
                if peak is None or rate > peak:
                    peak = rate
    return (peak if peak is not None else average), average


# ------------------------------------------------------------- codec string

def avc_codec_string(init_bytes):
    """Read profile / constraint flags / level from the avcC box of an fMP4 init segment."""
    index = init_bytes.find(b"avcC")
    if index < 0 or len(init_bytes) < index + 8:
        raise ValueError("no avcC box found in init segment")
    profile, compat, level = init_bytes[index + 5], init_bytes[index + 6], init_bytes[index + 7]
    return f"avc1.{profile:02X}{compat:02X}{level:02X}"


AAC_LC_CODEC = "mp4a.40.2"


def format_frame_rate(fps):
    return f"{float(fps):.3f}"
