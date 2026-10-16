#!/usr/bin/env python3
"""Bitrate quality trial: encode the same excerpts at several bitrates and compare them.

Uses the production encoder settings from make_hls.py (H.264 Main, 2 s GOP, two-pass VBR with
maxrate/bufsize caps), so what you see here is what the HLS ladder will deliver.

For each excerpt (start, middle, and the most demanding window found by scanning the source's own
packet sizes) and each (resolution, bitrate) it reports VMAF, SSIM, size and the projected size of a
30-minute video, and saves the worst-scoring frame next to the source frame so you can judge by eye.

Open <out>/index.html when it finishes. Numbers inform; your eyes decide.

Usage: python3 bitrate_trial.py INPUT [-o OUT_DIR] [--excerpt-seconds 20] [--preset slow]
"""

import argparse
import html
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from fractions import Fraction

import hls_common as common
import make_hls

# (resolution short side, [bitrates in kbps]); a CRF reference is added to each resolution.
TRIAL_PLAN = [
    (360, [400, 600]),
    (720, [1100, 1500, 2000]),
    (1080, [2500, 3500, 5000]),
]
CRF_REFERENCE = {"crf": 20, "cap_kbps": {360: 1500, 720: 5000, 1080: 9000}}
PROJECT_SECONDS = 30 * 60
AUDIO_KBPS = 128


def scan_demanding_window(path, window, step=5):
    """Return the start time of the window with the most video bytes (a proxy for motion/detail)."""
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time,size",
           "-of", "csv=p=0", path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    per_second = {}
    for line in result.stdout.splitlines():
        parts = line.split(",")
        if len(parts) >= 2 and parts[0] not in ("", "N/A"):
            second = int(float(parts[0]))
            per_second[second] = per_second.get(second, 0) + int(parts[1])
    if not per_second:
        return 0.0
    last = max(per_second)
    best_start, best_bytes = 0, -1
    for start in range(0, max(1, last - window + 1), step):
        total = sum(per_second.get(s, 0) for s in range(start, start + window))
        if total > best_bytes:
            best_start, best_bytes = start, total
    return float(best_start)


def choose_excerpts(path, duration, window):
    if duration <= window:
        return [("whole", 0.0)]
    picks = [("start", min(10.0, max(0.0, duration - window))),
             ("middle", max(0.0, duration / 2 - window / 2)),
             ("demanding", scan_demanding_window(path, int(window)))]
    unique, seen = [], []
    for label, start in picks:
        if all(abs(start - other) >= window / 2 for other in seen):
            unique.append((label, start))
            seen.append(start)
    return unique


def encode(src, start, length, r, fps, preset, out_path, work_dir, crf_cap=None):
    """Encode an excerpt with production settings. crf_cap=(crf, cap_kbps) switches to capped-CRF."""
    flt = make_hls.video_filter(r, fps)
    base = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
            "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", src, "-map", "0:v:0", "-vf", flt]
    if crf_cap:
        crf, cap = crf_cap
        args = make_hls.video_args(r, fps, preset)
        # replace the target bitrate with CRF + cap
        drop = {"-b:v", "-maxrate", "-bufsize"}
        cleaned = []
        skip = False
        for item in args:
            if skip:
                skip = False
                continue
            if item in drop:
                skip = True
                continue
            cleaned.append(item)
        cmd = base + cleaned + ["-crf", str(crf), "-maxrate", f"{cap}k", "-bufsize", f"{cap * 2}k", "-an", out_path]
        return subprocess.run(cmd).returncode == 0
    passlog = os.path.join(work_dir, "pass")
    p1 = base + make_hls.video_args(r, fps, preset, 1, passlog) + ["-an", "-f", "null", os.devnull]
    p2 = base + make_hls.video_args(r, fps, preset, 2, passlog) + ["-an", out_path]
    ok = subprocess.run(p1).returncode == 0 and subprocess.run(p2).returncode == 0
    for leftover in os.listdir(work_dir):
        if leftover.startswith("pass"):
            os.remove(os.path.join(work_dir, leftover))
    return ok


def measure(enc_path, src, start, length, r, fps, work_dir):
    """VMAF (mean/min/worst-frame) and SSIM of an encode against the source scaled to the same size."""
    log = "vmaf.json"
    ref_chain = (f"fps={fps.numerator}/{fps.denominator},scale={r.width}:{r.height}:flags=lanczos,"
                 "setsar=1,setpts=PTS-STARTPTS")
    graph = (f"[0:v]setpts=PTS-STARTPTS[dist];[1:v]{ref_chain}[ref];"
             f"[dist][ref]libvmaf=log_fmt=json:log_path={log}:n_threads=8:feature=name=float_ssim")
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-i", os.path.abspath(enc_path),
           "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", os.path.abspath(src),
           "-lavfi", graph, "-f", "null", "-"]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=work_dir)
    log_path = os.path.join(work_dir, log)
    if result.returncode != 0 or not os.path.isfile(log_path):
        return {"error": (result.stderr.strip().splitlines() or ["libvmaf failed"])[-1]}
    with open(log_path, encoding="utf-8") as handle:
        data = json.load(handle)
    os.remove(log_path)
    pooled = data.get("pooled_metrics", {})
    frames = data.get("frames", [])
    worst = min(frames, key=lambda f: f["metrics"]["vmaf"]) if frames else None
    return {
        "vmaf_mean": pooled.get("vmaf", {}).get("mean"),
        "vmaf_min": pooled.get("vmaf", {}).get("min"),
        "vmaf_p5": percentile([f["metrics"]["vmaf"] for f in frames], 5),
        "ssim_mean": pooled.get("float_ssim", {}).get("mean"),
        "worst_frame": worst["frameNum"] if worst else None,
    }


def percentile(values, pct):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * pct / 100))]


def save_frame(video, timestamp, out_png):
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-ss", f"{timestamp:.3f}",
           "-i", video, "-frames:v", "1", out_png]
    return subprocess.run(cmd).returncode == 0


def save_reference_frame(src, timestamp, r, out_png):
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-ss", f"{timestamp:.3f}",
           "-i", src, "-frames:v", "1", "-vf", f"scale={r.width}:{r.height}:flags=lanczos", out_png]
    return subprocess.run(cmd).returncode == 0


def write_reports(out_dir, source, results, groups):
    with open(os.path.join(out_dir, "results.json"), "w", encoding="utf-8") as handle:
        json.dump(results, handle, indent=2)

    def num(value, digits=1):
        return "-" if value is None else f"{value:.{digits}f}"

    lines = [f"# Bitrate trial: {os.path.basename(source)}", "",
             "Projected 30-min size = measured video bitrate x 1800 s (+128 kbps audio).", ""]
    html_parts = ["<!doctype html><meta charset=utf-8><title>Bitrate trial</title>"
                  "<style>body{font:14px system-ui;margin:24px;max-width:1500px}table{border-collapse:collapse;margin:12px 0}"
                  "td,th{border:1px solid #ccc;padding:4px 8px;text-align:right}th{background:#f3f3f3}"
                  ".grid{display:flex;flex-wrap:wrap;gap:10px}figure{margin:0}img{max-width:460px;display:block}"
                  "figcaption{font-size:12px}h3{margin-top:36px}</style>",
                  f"<h1>Bitrate trial: {html.escape(os.path.basename(source))}</h1>",
                  "<p>Compare the frames below at full size. VMAF above ~93 is usually indistinguishable on a phone; "
                  "check the posture/hand/foot detail with your own eyes.</p>"]
    for (res, label), rows in groups.items():
        lines += [f"## {res}p, excerpt '{label}'", "",
                  "| Setting | Video kbps (measured) | VMAF mean | VMAF 5th pct | VMAF min | SSIM | 30-min size |",
                  "|---|---|---|---|---|---|---|"]
        html_parts += [f"<h3>{res}p &middot; excerpt '{html.escape(label)}'</h3><table><tr><th>Setting</th>"
                       "<th>Video kbps</th><th>VMAF mean</th><th>VMAF 5th pct</th><th>VMAF min</th><th>SSIM</th>"
                       "<th>30-min size</th></tr>"]
        for row in rows:
            m = row.get("metrics", {})
            if row["video_kbps"] > 0:
                size = f"{(row['video_kbps'] + AUDIO_KBPS) * PROJECT_SECONDS / 8000:.0f} MB"
            else:
                size = "-"          # the source reference frame has no bitrate of its own
            cells = [row["setting"], num(row["video_kbps"] or None, 0), num(m.get("vmaf_mean")),
                     num(m.get("vmaf_p5")), num(m.get("vmaf_min")), num(m.get("ssim_mean"), 4), size]
            lines.append("| " + " | ".join(cells) + " |")
            html_parts.append("<tr>" + "".join(f"<td>{html.escape(c)}</td>" for c in cells) + "</tr>")
        lines.append("")
        html_parts.append("</table><div class=grid>")
        for row in rows:
            if row.get("frame"):
                html_parts.append(f"<figure><a href='{row['frame']}'><img src='{row['frame']}' loading=lazy></a>"
                                  f"<figcaption>{html.escape(row['setting'])}</figcaption></figure>")
        html_parts.append("</div>")
    with open(os.path.join(out_dir, "results.md"), "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as handle:
        handle.write("\n".join(html_parts))


def main():
    parser = argparse.ArgumentParser(description="Compare H.264 bitrates on your own footage.")
    parser.add_argument("input")
    parser.add_argument("-o", "--out-dir", help="Output folder (default: <input folder>/bitrate_trial)")
    parser.add_argument("--excerpt-seconds", type=int, default=20)
    parser.add_argument("--preset", default="slow")
    parser.add_argument("--resolutions", default="360,720,1080",
                        help="Comma-separated short sides to test (default: 360,720,1080)")
    args = parser.parse_args()
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit(f"Error: {tool} not found")
    if "libvmaf" not in subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout:
        sys.exit("Error: this ffmpeg build has no libvmaf filter")

    info = make_hls.probe_source(args.input)
    fps = make_hls.choose_fps(info.fps)
    out_dir = args.out_dir or os.path.join(os.path.dirname(os.path.abspath(args.input)), "bitrate_trial")
    os.makedirs(os.path.join(out_dir, "frames"), exist_ok=True)
    wanted = {int(x) for x in args.resolutions.split(",")}

    excerpts = choose_excerpts(args.input, info.duration, args.excerpt_seconds)
    length = min(float(args.excerpt_seconds), info.duration)
    print(f"Source {info.width}x{info.height} @ {float(info.fps):.3f} fps, {info.duration:.0f}s. "
          f"Excerpts: " + ", ".join(f"{label}@{start:.0f}s" for label, start in excerpts))

    results, groups = [], {}
    started = time.time()
    with tempfile.TemporaryDirectory(prefix="trial_") as work:
        for label, start in excerpts:
            for short, bitrates in TRIAL_PLAN:
                if short not in wanted or min(info.width, info.height) < short * make_hls.LADDER_TOLERANCE:
                    continue
                width, height = make_hls.scaled_dims(info.width, info.height, short)
                settings = [(f"{kbps} kbps", kbps, None) for kbps in bitrates]
                cap = CRF_REFERENCE["cap_kbps"][short]
                settings.append((f"CRF {CRF_REFERENCE['crf']} (cap {cap} kbps)", cap, (CRF_REFERENCE["crf"], cap)))
                rows = []
                worst_time = None
                for name, kbps, crf_cap in settings:
                    r = make_hls.Rendition(f"{short}p", width, height, kbps)
                    tag = f"{label}_{short}p_{'crf' if crf_cap else str(kbps) + 'k'}"
                    out_path = os.path.join(out_dir, f"{tag}.mp4")
                    print(f"  [{time.time() - started:5.0f}s] {label} {short}p {name} ...", flush=True)
                    if not encode(args.input, start, length, r, fps, args.preset, out_path, work, crf_cap):
                        print("     encode failed, skipping")
                        continue
                    seconds = common.ffprobe_json(out_path, "-show_entries", "format=duration,size")["format"]
                    measured = 8 * float(seconds["size"]) / float(seconds["duration"]) / 1000
                    metrics = measure(out_path, args.input, start, length, r, fps, work)
                    if "error" in metrics:
                        print(f"     measure failed: {metrics['error']}")
                    rows.append({"setting": name, "video_kbps": measured, "metrics": metrics, "file": out_path,
                                 "r": r, "tag": tag})
                    print(f"     {measured:.0f} kbps, VMAF {metrics.get('vmaf_mean')}", flush=True)

                if not rows:
                    continue
                # Worst frame of the lowest-bitrate encode, shown for every setting next to the source.
                lowest = min((x for x in rows if x["metrics"].get("worst_frame") is not None),
                             key=lambda x: x["video_kbps"], default=None)
                if lowest is not None:
                    worst_time = lowest["metrics"]["worst_frame"] / float(fps)
                    ref_png = os.path.join(out_dir, "frames", f"{label}_{short}p_source.png")
                    if save_reference_frame(args.input, start + worst_time, lowest["r"], ref_png):
                        rows.insert(0, {"setting": f"SOURCE (scaled to {width}x{height}) at {worst_time:.1f}s",
                                        "video_kbps": 0, "metrics": {}, "frame": f"frames/{os.path.basename(ref_png)}"})
                    for row in rows:
                        if "file" in row:
                            png = os.path.join(out_dir, "frames", f"{row['tag']}.png")
                            if save_frame(row["file"], worst_time, png):
                                row["frame"] = f"frames/{os.path.basename(png)}"
                clean = [{k: v for k, v in row.items() if k not in ("r",)} for row in rows]
                groups[(short, label)] = [row for row in clean if row["video_kbps"] > 0 or row.get("frame")]
                results.append({"excerpt": label, "start": start, "resolution": short, "rows": clean})
                write_reports(out_dir, args.input, results, {k: [r for r in v if r["video_kbps"] > 0 or "frame" in r]
                                                              for k, v in groups.items()})
    print(f"\nDone in {(time.time() - started) / 60:.1f} min. Open: {os.path.join(out_dir, 'index.html')}")
    print(f"Summary table: {os.path.join(out_dir, 'results.md')}")


if __name__ == "__main__":
    main()
