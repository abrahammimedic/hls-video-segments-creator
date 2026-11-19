"""Pick a poster frame (the thumbnail) from a video and save it as a small JPEG.

The poster is made when the video is encoded and published next to it
(`<version folder>/poster.jpg`), so there is nothing to upload by hand and it
can never be out of step with the video.

Automatic pick (like Instagram's default cover): look at a few short windows of
the video (just after the start, a quarter of the way in, halfway), let ffmpeg's
`thumbnail` filter choose the most representative frame of each window, and take
the first one that is not (nearly) black or white. `--poster-at SECONDS` skips
all that and takes the frame at that time.

JPEG, not WebP: the ffmpeg builds most people have carry no WebP encoder, and a
540-pixel JPEG is small enough (about 40-90 KB).
"""

import os
import subprocess
import tempfile

POSTER_NAME = "poster.jpg"
POSTER_SHORT_SIDE = 540          # pixels on the short side; never upscaled
WINDOW_SECONDS = 6               # how much of the video each automatic candidate is chosen from
THUMBNAIL_BATCH = 100            # frames ffmpeg's `thumbnail` filter compares (about 4 s at 25 fps)
MIN_LUMA, MAX_LUMA = 24, 232     # average brightness (0-255) outside which a frame counts as black / white
MAX_BYTES = 250 * 1024           # the quality is lowered until the file fits
QUALITIES = (4, 6, 8, 11)        # ffmpeg -q:v steps (lower is better)


def _even(value):
    return max(2, 2 * round(value / 2))


def poster_size(display_w, display_h, short_side=POSTER_SHORT_SIDE):
    """(width, height) with the short side at most `short_side`, aspect ratio and orientation kept, both even."""
    short = min(short_side, display_w, display_h)
    if display_w >= display_h:
        return _even(display_w * short / display_h), _even(short)
    return _even(short), _even(display_h * short / display_w)


def candidate_starts(duration, at=None):
    """Seconds at which to look for a poster: one if `at` is given, else a few spread through the video."""
    if at is not None:
        return [float(at)]
    if duration <= 3:
        return [0.0]
    starts = [1.0, duration * 0.25, duration * 0.5]
    latest = max(0.0, duration - 1.0)
    out = []
    for start in starts:
        start = round(min(start, latest), 2)
        if start not in out:
            out.append(start)
    return out


def _run(cmd):
    return subprocess.run(cmd, capture_output=True)


def frame_command(input_path, start, size, out_path, quality, pick_best):
    """The ffmpeg command that writes one frame as a JPEG.

    With `pick_best`, ffmpeg chooses the most representative frame of the next
    few seconds; without it, it takes exactly the frame at `start`.
    """
    width, height = size
    scale = f"scale={width}:{height}:flags=lanczos,setsar=1"
    vf = f"thumbnail={THUMBNAIL_BATCH},{scale}" if pick_best else scale
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-ss", f"{start:.3f}"]
    if pick_best:
        cmd += ["-t", str(WINDOW_SECONDS)]
    return cmd + ["-i", input_path, "-an", "-map_metadata", "-1", "-vf", vf, "-frames:v", "1",
                  "-q:v", str(quality), out_path]


def mean_luma(image_path):
    """Average brightness of an image, 0 (black) to 255 (white), or None if it cannot be read."""
    result = _run(["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-i", image_path,
                   "-vf", "scale=1:1", "-f", "rawvideo", "-pix_fmt", "gray", "-"])
    if result.returncode != 0 or not result.stdout:
        return None
    return result.stdout[0]


def make_poster(input_path, display_w, display_h, duration, out_path, at=None, log=print):
    """Write the poster for `input_path` to `out_path`. Returns {"time", "luma", "bytes", "size"}.

    Raises RuntimeError if no frame could be written (or `at` is past the end).
    """
    if at is not None and duration and at >= duration:
        raise RuntimeError(f"--poster-at {at} is past the end of the video ({duration:.1f}s)")
    size = poster_size(display_w, display_h)
    best = None   # (distance from mid-grey, start, luma, path) for the fallback
    scratch = tempfile.mkdtemp(prefix="poster_")
    try:
        for index, start in enumerate(candidate_starts(duration, at)):
            candidate = os.path.join(scratch, f"c{index}.jpg")
            if _run(frame_command(input_path, start, size, candidate, QUALITIES[0], at is None)).returncode != 0 \
                    or not os.path.isfile(candidate):
                continue
            luma = mean_luma(candidate)
            distance = 0 if luma is None else abs(luma - 128)
            if best is None or distance < best[0]:
                best = (distance, start, luma, candidate)
            if at is not None or luma is None or MIN_LUMA <= luma <= MAX_LUMA:
                best = (distance, start, luma, candidate)
                break
        if best is None:
            raise RuntimeError(f"could not take a poster frame from {input_path}")
        _, start, luma, chosen = best
        if luma is not None and not (MIN_LUMA <= luma <= MAX_LUMA):
            log(f"   (poster: every candidate frame is very {'dark' if luma < MIN_LUMA else 'bright'}; "
                f"using the least extreme, at {start:g}s. Pick another with --poster-at)")
        # Re-encode with the quality ladder until the file is small enough (usually the first try).
        final = chosen
        for quality in QUALITIES[1:]:
            if os.path.getsize(final) <= MAX_BYTES:
                break
            retry = os.path.join(scratch, f"q{quality}.jpg")
            if _run(frame_command(input_path, start, size, retry, quality, at is None)).returncode == 0 \
                    and os.path.isfile(retry):
                final = retry
        os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
        with open(final, "rb") as src, open(out_path, "wb") as dst:
            dst.write(src.read())
        return {"time": start, "luma": luma, "bytes": os.path.getsize(out_path), "size": size}
    finally:
        for name in os.listdir(scratch):
            os.remove(os.path.join(scratch, name))
        os.rmdir(scratch)
