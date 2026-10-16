#!/usr/bin/env python3
"""Compress a single video file with ffmpeg using manual codec/resolution/quality settings."""

import argparse
import os
import shutil
import subprocess
import sys

RESOLUTIONS = {
    "360p": "640:360",
    "720p": "1280:720",
    "1080p": "1920:1080",
}

CODECS = {
    "h264": ("libx264", "mp4"),
    "h265": ("libx265", "mp4"),
    "vp9": ("libvpx-vp9", "webm"),
}

PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast",
           "medium", "slow", "slower", "veryslow"]


def human_size(num_bytes):
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"


def build_scale_filter(resolution):
    if resolution in (None, "original"):
        return None
    if resolution in RESOLUTIONS:
        width, height = RESOLUTIONS[resolution].split(":")
    elif "x" in resolution.lower():
        width, height = resolution.lower().split("x", 1)
    else:
        raise ValueError(f"Unknown resolution '{resolution}'. Use 360p, 720p, 1080p, original, or WxH.")
    # The target is a landscape box. Portrait input swaps it (720p -> 720x1280), and
    # force_original_aspect_ratio=decrease fits inside it so the video is never stretched.
    # force_divisible_by=2 keeps dimensions even, which codecs require.
    return (f"scale=w='if(gt(iw,ih),{width},{height})':h='if(gt(iw,ih),{height},{width})'"
            ":force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1")


def build_command(args, output_path, codec_name, extension):
    cmd = ["ffmpeg", "-hide_banner", "-i", args.input, "-c:v", codec_name]

    if args.bitrate:
        cmd += ["-b:v", args.bitrate]
    else:
        cmd += ["-crf", str(args.crf)]
        # VP9 needs -b:v 0 to let CRF mode take effect.
        if codec_name == "libvpx-vp9":
            cmd += ["-b:v", "0"]

    scale = build_scale_filter(args.resolution)
    if scale:
        cmd += ["-vf", scale]

    if codec_name in ("libx264", "libx265"):
        cmd += ["-preset", args.preset]
        if codec_name == "libx265":
            cmd += ["-tag:v", "hvc1"]  # lets H.265 play in Apple devices / QuickTime
        cmd += ["-movflags", "+faststart"]

    cmd += ["-c:a", "libopus" if extension == "webm" else "aac", "-b:a", args.audio_bitrate]
    cmd += ["-y" if args.overwrite else "-n", output_path]
    return cmd


def parse_args():
    parser = argparse.ArgumentParser(
        description="Compress a video file with ffmpeg.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("input", help="Path to the input video file")
    parser.add_argument("-o", "--output", help="Output file path (default: <input>_compressed.<ext>)")
    parser.add_argument("--resolution", default="original",
                        help="360p, 720p, 1080p, original, or WIDTHxHEIGHT (e.g. 1280x720)")
    parser.add_argument("--codec", choices=sorted(CODECS), default="h264",
                        help="Video codec: h264 (most compatible), h265 (smaller files), vp9 (webm)")
    parser.add_argument("--bitrate", help="Target video bitrate, e.g. 2M or 1500k (overrides --crf)")
    parser.add_argument("--crf", type=int, default=23,
                        help="Constant quality, 0-51 for h264/h265 (lower = better/bigger). Ignored if --bitrate is set")
    parser.add_argument("--audio-bitrate", default="128k", help="Audio bitrate")
    parser.add_argument("--preset", choices=PRESETS, default="medium",
                        help="Encoding speed vs. compression efficiency (h264/h265 only)")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite the output file if it exists")
    return parser.parse_args()


def main():
    args = parse_args()

    if not shutil.which("ffmpeg"):
        sys.exit("Error: ffmpeg not found. Install it with: brew install ffmpeg")

    if not os.path.isfile(args.input):
        sys.exit(f"Error: input file not found: {args.input}")

    codec_name, extension = CODECS[args.codec]
    stem, _ = os.path.splitext(args.input)
    output_path = args.output or f"{stem}_compressed.{extension}"

    if os.path.exists(output_path) and not args.overwrite:
        sys.exit(f"Error: output already exists: {output_path} (use --overwrite to replace it)")

    try:
        cmd = build_command(args, output_path, codec_name, extension)
    except ValueError as err:
        sys.exit(f"Error: {err}")

    input_size = os.path.getsize(args.input)
    print(f"Input:  {args.input} ({human_size(input_size)})")
    print(f"Output: {output_path}")
    print(f"Codec:  {args.codec} | Resolution: {args.resolution} | "
          f"Quality: {'bitrate ' + args.bitrate if args.bitrate else 'CRF ' + str(args.crf)}")
    print()

    # stdout/stderr are inherited so ffmpeg's progress is shown live.
    result = subprocess.run(cmd)
    if result.returncode != 0:
        sys.exit(f"\nffmpeg failed with exit code {result.returncode}")

    output_size = os.path.getsize(output_path)
    reduction = (1 - output_size / input_size) * 100
    print()
    print(f"Done. {human_size(input_size)} -> {human_size(output_size)} "
          f"({reduction:.1f}% smaller)")


if __name__ == "__main__":
    main()
