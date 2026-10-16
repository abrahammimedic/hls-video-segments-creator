#!/usr/bin/env python3
"""Create the standard (720p) and data saver (360p) web versions of one or more videos.

Both versions use H.264 Main profile, two-pass VBR with a capped bitrate,
a 2-second keyframe interval, and FastStart (moov atom moved to the front
so the video can start playing before it fully downloads).
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile

PROFILES = {
    "standard": {
        "label": "720p Standard",
        "width": 1280,
        "height": 720,
        "level": "3.1",
        "fps": 30,
        "bitrate_kbps": 1100,     # target, midpoint of 1000-1200 kbps
        "audio_bitrate": "128k",
    },
    "data_saver": {
        "label": "360p Data Saver",
        "width": 640,
        "height": 360,
        "level": "3.0",
        "fps": 25,
        "bitrate_kbps": 350,      # target, midpoint of 300-400 kbps
        "audio_bitrate": "96k",
    },
}

KEYFRAME_INTERVAL_SECONDS = 2
MAXRATE_MULTIPLIER = 1.5          # cap peaks at 1.5x the target
BUFSIZE_MULTIPLIER = 2            # VBV buffer = 2x the target


def human_size(num_bytes):
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"


def scale_filter(profile):
    # Landscape input -> width x height; portrait input -> height x width (e.g. 720x1280).
    # force_original_aspect_ratio=decrease avoids stretching non-16:9 sources.
    w, h = profile["width"], profile["height"]
    return (f"scale=w='if(gt(iw,ih),{w},{h})':h='if(gt(iw,ih),{h},{w})'"
            ":force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1")


def base_args(input_path, profile):
    fps = profile["fps"]
    target = profile["bitrate_kbps"]
    return [
        "ffmpeg", "-hide_banner", "-i", input_path,
        "-vf", scale_filter(profile),
        "-r", str(fps),
        "-c:v", "libx264",
        "-profile:v", "main",
        "-level:v", profile["level"],
        "-pix_fmt", "yuv420p",
        "-b:v", f"{target}k",
        "-maxrate", f"{int(target * MAXRATE_MULTIPLIER)}k",
        "-bufsize", f"{int(target * BUFSIZE_MULTIPLIER)}k",
        # Keyframe every 2 seconds. -keyint_min matches it so the GOP stays fixed.
        "-g", str(fps * KEYFRAME_INTERVAL_SECONDS),
        "-keyint_min", str(fps * KEYFRAME_INTERVAL_SECONDS),
        "-sc_threshold", "0",
        "-preset", "slow",
    ]


def encode_two_pass(input_path, output_path, profile):
    log_dir = tempfile.mkdtemp(prefix="passlog_")
    passlog = os.path.join(log_dir, "ffmpeg2pass")
    try:
        # Pass 1: analysis only, no output file.
        pass1 = base_args(input_path, profile) + [
            "-pass", "1", "-passlogfile", passlog,
            "-an", "-f", "null", os.devnull,
        ]
        print("  Pass 1/2 (analysis)...")
        if subprocess.run(pass1).returncode != 0:
            return False

        # Pass 2: real encode with audio and FastStart.
        pass2 = base_args(input_path, profile) + [
            "-pass", "2", "-passlogfile", passlog,
            "-c:a", "aac", "-b:a", profile["audio_bitrate"],
            "-movflags", "+faststart",
            "-y", output_path,
        ]
        print("  Pass 2/2 (encoding)...")
        return subprocess.run(pass2).returncode == 0
    finally:
        shutil.rmtree(log_dir, ignore_errors=True)


def make_output_path(input_path, profile, out_dir):
    stem = os.path.splitext(os.path.basename(input_path))[0]
    return os.path.join(out_dir, f"{stem} - {profile['label']}.mp4")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Create 720p Standard and 360p Data Saver versions of videos.",
    )
    parser.add_argument("inputs", nargs="+", help="One or more input video files")
    parser.add_argument("-d", "--out-dir",
                        help="Folder for outputs (default: same folder as each input)")
    parser.add_argument("--only", choices=sorted(PROFILES),
                        help="Create only one version instead of both")
    return parser.parse_args()


def main():
    args = parse_args()

    if not shutil.which("ffmpeg"):
        sys.exit("Error: ffmpeg not found. Install it with: brew install ffmpeg")

    profiles = [args.only] if args.only else ["standard", "data_saver"]
    failures = []

    for input_path in args.inputs:
        if not os.path.isfile(input_path):
            print(f"Skipping, file not found: {input_path}")
            failures.append(input_path)
            continue

        out_dir = args.out_dir or os.path.dirname(os.path.abspath(input_path))
        os.makedirs(out_dir, exist_ok=True)
        input_size = os.path.getsize(input_path)
        print(f"\n== {os.path.basename(input_path)} ({human_size(input_size)})")

        for key in profiles:
            profile = PROFILES[key]
            output_path = make_output_path(input_path, profile, out_dir)
            print(f"\n[{profile['label']}] -> {output_path}")
            if encode_two_pass(input_path, output_path, profile):
                output_size = os.path.getsize(output_path)
                print(f"  Done: {human_size(output_size)} "
                      f"({(1 - output_size / input_size) * 100:.1f}% smaller)")
            else:
                print("  FAILED")
                failures.append(f"{input_path} [{profile['label']}]")

    if failures:
        print("\nFailed:")
        for item in failures:
            print(f"  - {item}")
        sys.exit(1)
    print("\nAll versions created.")


if __name__ == "__main__":
    main()
