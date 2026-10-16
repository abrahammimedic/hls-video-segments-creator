# HLS Video Segments Creator

A command-line toolkit that turns a video file into **adaptive HLS streaming segments** (360p / 720p / 1080p, 6-second fMP4 chunks), validates them, and publishes them to **Cloudflare R2** so a mobile or web player can switch quality automatically as the viewer's bandwidth changes.

It was built for serving exercise videos to a Flutter app, but nothing in it is app-specific: any player that understands HLS (ExoPlayer, AVPlayer, hls.js, Safari) can play the output.

```
 source video                                                            player
 (.mov / .mp4)                                                       (Flutter, web)
      |                                                                     ^
      v                                                                     |
 make_hls.py ──> validate_hls.py ──> r2_upload.py ──> Cloudflare R2 ──> Cloudflare CDN
   encode 3        check playlists,     upload, then     (storage)       (custom domain
   qualities,      keyframes, A/V        re-validate                      + cache rule)
   write master    sync, bitrates        through the
   playlist                              public URL
```

## Contents

1. [What you get](#what-you-get)
2. [Requirements](#requirements)
3. [Quick start](#quick-start-5-minutes-no-cloud-account-needed)
4. [Publishing to Cloudflare R2](#publishing-to-cloudflare-r2)
5. [The release flow, step by step](#the-release-flow-step-by-step)
6. [Command reference](#command-reference)
7. [How it works](#how-it-works)
8. [Production checklist (Cloudflare)](#production-checklist-cloudflare)
9. [Validation](#validation)
10. [Choosing bitrates](#choosing-bitrates)
11. [Playing the result](#playing-the-result)
12. [Project layout](#project-layout)
13. [Tests](#tests)
14. [Troubleshooting](#troubleshooting)
15. [Legacy single-file tools](#legacy-single-file-tools)

---

## What you get

For one input video, `make_hls.py` produces an immutable, versioned folder:

```
hls_output/<slug>/v1/
  master.m3u8                 <- the only URL the player needs
  360p/   index.m3u8  init.mp4  seg_00000.m4s  seg_00001.m4s ...
  720p/   index.m3u8  init.mp4  seg_00000.m4s ...
  1080p/  index.m3u8  init.mp4  seg_00000.m4s ...
```

Behaviours worth knowing before you start:

- **Aspect ratio and orientation are always kept.** A portrait video stays portrait (360x640, 720x1280, 1080x1920). Nothing is ever stretched.
- **Never upscaled.** A 720p source produces 360p + 720p only. A source smaller than 360p produces one native-resolution rendition.
- **Quality switches are seamless.** Every rendition uses the same frame rate and a fixed 2-second keyframe interval, so all 6-second segment boundaries line up across qualities.
- **Versions are immutable.** Each run creates the next `vN`. A published version is never overwritten, so viewers mid-playback and CDN caches never see a mix of old and new files.
- **Resumable.** If an encode or upload is interrupted, re-run the same command and only the unfinished work is redone.
- **Silent videos and variable-frame-rate phone/screen recordings work** (the latter are converted to constant frame rate).

## Requirements

| Need | Version / notes |
|---|---|
| Python | 3.9 or newer (3.10+ recommended; recent `boto3` releases dropped 3.9) |
| ffmpeg + ffprobe | Built with `libx264` and `aac` (developed and tested with ffmpeg 9.0.2). `brew install ffmpeg` (macOS) or `apt install ffmpeg` (Linux). The bitrate trial additionally needs the `libvmaf` filter, which the Homebrew build includes |
| `boto3` | Only for uploading to R2: `pip3 install -r requirements.txt` |
| Cloudflare account | Only for publishing: an R2 bucket with a custom domain (see [Publishing](#publishing-to-cloudflare-r2)) |
| Apple HLS Tools | Optional, macOS only: `mediastreamvalidator` for an independent compliance check (see [Validation](#validation)) |

Encoding, validation and the bitrate trial use only the Python standard library plus ffmpeg, so you can try the whole encode-and-validate flow without any cloud account.

## Quick start (5 minutes, no cloud account needed)

```bash
git clone git@github.com:abrahammimedic/hls-video-segments-creator.git
cd hls-video-segments-creator

# 1. Check your tools
ffmpeg -version | head -1
python3 --version

# 2. Run the tests (about 40 seconds; builds a tiny real HLS stream with ffmpeg)
python3 -m unittest discover tests

# 3. Encode a video (writes next to the source, in <input folder>/hls_output/<name>/v1/)
python3 make_hls.py "/path/to/my video.mp4" --name my-video

# 4. Preview it locally in a browser or player
cd "/path/to/hls_output/my-video/v1" && python3 -m http.server 8000
#    then open http://localhost:8000/master.m3u8 in Safari, or paste the URL into VLC / hls.js
```

`make_hls.py` validates every build automatically and prints a PASSED/FAILED report. Encoding is deliberately high quality (two-pass x264, `slow` preset), so allow roughly 2x the video's duration for 1080p content. Use `--preset medium` or `--preset fast` for quicker, slightly larger results.

## Publishing to Cloudflare R2

You do this setup once.

### 1. Create the bucket and attach a custom domain

In the Cloudflare dashboard: **R2 Object Storage, Create bucket**, then in the bucket's **Settings, Custom Domains** connect a domain such as `video.example.com`. Always serve through the custom domain. The `r2.dev` URL is rate-limited and is not cached by the CDN.

### 2. Create an API token

1. R2 Object Storage, **Manage API tokens**, **Create API token** (account token if you are a Super Administrator, otherwise a user token).
2. Permissions: **Object Read & Write**.
3. Scope: **specific bucket**, and choose only your video bucket. Do not use an admin or global key.
4. Copy the **Access Key ID** and **Secret Access Key** immediately. The secret is shown once. (Ignore the separate "Token value".)

### 3. Store the credentials outside the repo

Credentials are read from environment variables only. Keep them in a private file in your home directory, never in the project:

```bash
mkdir -p ~/.config/video-tool && chmod 700 ~/.config/video-tool
cat > ~/.config/video-tool/r2.env <<'EOF'
R2_ACCOUNT_ID=your_cloudflare_account_id
R2_ACCESS_KEY_ID=your_access_key_id
R2_SECRET_ACCESS_KEY=your_secret_access_key
R2_BUCKET=your-bucket-name
R2_PUBLIC_BASE_URL=https://video.example.com
EOF
chmod 600 ~/.config/video-tool/r2.env
```

No quotes, no spaces around `=`, and no trailing slash on the URL. Load them into your shell before uploading:

```bash
set -a; . ~/.config/video-tool/r2.env; set +a
```

| Variable | Where to find it |
|---|---|
| `R2_ACCOUNT_ID` | R2 overview page, right-hand side (also the hex part of the bucket's S3 URL) |
| `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` | Shown when you create the API token |
| `R2_BUCKET` | Your bucket's name |
| `R2_PUBLIC_BASE_URL` | The custom domain from step 1. Used for the post-upload check and the printed player URL |

### 4. Install the upload dependency

```bash
pip3 install -r requirements.txt
```

## The release flow, step by step

Use a real example: publishing `lesson.mov` into the bucket folder `courses/week_1/lesson`.

```bash
# 0. Load credentials (once per terminal session)
set -a; . ~/.config/video-tool/r2.env; set +a

# 1. Encode and validate locally
python3 make_hls.py "/path/to/lesson.mov" --name lesson

# 2. Rehearse the upload. Reads the bucket, uploads nothing, shows the exact destination
python3 r2_upload.py /path/to/hls_output/lesson/v1 --path courses/week_1/lesson --dry-run

# 3. Upload for real. It verifies the bucket against local files, then validates
#    again through your public domain and prints the master URL
python3 r2_upload.py /path/to/hls_output/lesson/v1 --path courses/week_1/lesson
```

Step 3 ends with:

```
Published and validated.
Master URL: https://video.example.com/courses/week_1/lesson/v1/master.m3u8
```

4. **Test playback** in your real app or player, ideally once on a throttled network to watch quality step up and down.
5. **Switch your application** (database, CMS, config) to the Master URL. The tool never touches your application's data.

Key rules:

- `--path` is **required** and has no default. You choose the bucket folder, so videos stay organised by your own feature folders. The tool appends `/v<N>/` automatically.
- To change a video later, run `make_hls.py` again (it creates `v2`) and upload it. Viewers still watching `v1` are unaffected.
- `master.m3u8` is uploaded **last**. A version without it is partial and unplayable.
- Only `master.m3u8`, `index.m3u8`, `init.mp4` and `seg_*.m4s` are ever uploaded. Work files live in `hls_output/<slug>/.work/` and are never sent.
- You can encode and upload in one command: `python3 make_hls.py video.mov --name lesson --upload --remote-path courses/week_1/lesson` (add `--dry-run` to rehearse).

### Retiring old versions

The tool never deletes anything. The one-year cache header is a cache hint, not a reason to keep old versions for a year. Delete an old version only when nothing can still reference it:

- Keep it for a grace period after switching your app (suggested 7-14 days): longer than your longest video, plus paused or resumed sessions, plus app screens that cached the old URL.
- Never delete the version your app currently points at.
- Deleting from R2 does **not** purge the CDN. Cached copies may keep serving for a while, but on a cache miss the file is gone, so treat deletion as a real cutoff.
- Do not use age-based R2 lifecycle rules on the folders that hold HLS versions; they could delete the live version.

## Command reference

### `make_hls.py`: encode, validate, optionally upload

```bash
python3 make_hls.py INPUT [INPUT ...] [options]
```

| Option | Default | Description |
|---|---|---|
| `-d, --out-dir` | `<input folder>/hls_output` | Output root |
| `--name` | derived from the file name | Slug for the local folder `<out>/<slug>/` (single input only) |
| `--version N` | next unused number | Explicit version. An unfinished local version is resumed |
| `--only 360p,720p` | all that the source supports | Build only some renditions |
| `--preset` | `slow` | x264 preset: `ultrafast` ... `veryslow`. Slower is smaller/better |
| `--force` | off | Re-encode even if finished renditions look complete |
| `--upload` | off | Upload after a successful validation (needs credentials and `--remote-path`) |
| `--remote-path` | none | With `--upload`: bucket folder, e.g. `courses/week_1/lesson` |
| `--dry-run` | off | With `--upload`: show what would be uploaded without touching R2 |

### `r2_upload.py`: upload a built version

```bash
python3 r2_upload.py VERSION_DIR --path BUCKET/FOLDER [options]
```

| Option | Description |
|---|---|
| `VERSION_DIR` | Local folder named `v<N>`, e.g. `hls_output/lesson/v1` |
| `--path` | **Required.** Bucket folder to publish into (letters, digits, `.`, `_`, `-`, separated by `/`) |
| `--dry-run` | Show the destination and files; upload nothing |
| `--resume` | Finish a partially uploaded version (no `master.m3u8` in R2 yet) |
| `--skip-validate` | Skip the local validation before uploading |

It refuses to upload a version whose `master.m3u8` already exists in the bucket, and it checks object counts and sizes after uploading.

### `validate_hls.py`: validate a folder or a URL

```bash
python3 validate_hls.py hls_output/lesson/v1                     # deep local check
python3 validate_hls.py https://video.example.com/courses/week_1/lesson/v1/master.m3u8 --local hls_output/lesson/v1
python3 validate_hls.py hls_output/lesson/v1 --apple-validate    # also runs Apple's validator
```

| Option | Description |
|---|---|
| `--local DIR` | With a URL: compare remote file sizes with a local folder |
| `--no-decode` | Skip the full decode and random seek tests (faster) |
| `--apple-validate` | Also run `mediastreamvalidator` if installed |

Exit code is `0` on success and `1` on any failure, so it is usable in CI.

### `bitrate_trial.py`: compare bitrates on your own footage

```bash
python3 bitrate_trial.py "/path/to/video.mp4" -o bitrate_trial
open bitrate_trial/index.html
```

| Option | Default | Description |
|---|---|---|
| `-o, --out-dir` | `<input folder>/bitrate_trial` | Output folder |
| `--excerpt-seconds` | `20` | Length of each test excerpt |
| `--preset` | `slow` | x264 preset (match production) |
| `--resolutions` | `360,720,1080` | Which resolutions to test |

## How it works

**The bitrate ladder** lives in one table at the top of `make_hls.py` (`LADDER`):

| Rendition | Video bitrate | Notes |
|---|---|---|
| 360p | 400 kbps | Chosen with `bitrate_trial.py` on real footage |
| 720p | 1500 kbps | |
| 1080p | 3500 kbps | |

Audio is AAC-LC, 128 kbps, 48 kHz stereo in every rendition. Change the table to retune, and use `bitrate_trial.py` first to judge the effect by eye.

**Encoding.** Each rendition is encoded with two-pass VBR H.264 (Main profile) capped at 1.5x the target bitrate, then cut into 6-second fragmented-MP4 segments (CMAF). The frame rate is forced constant, and the keyframe interval is exactly 2 seconds so every segment starts on a keyframe.

**The master playlist** is written by this tool, not by ffmpeg, so it is exact: `BANDWIDTH` and `AVERAGE-BANDWIDTH` are computed from the real segment sizes following RFC 8216, `CODECS` is read from the encoded file's own header, and `RESOLUTION` / `FRAME-RATE` come from the output.

**Resume safety.** Each rendition is encoded into a temporary folder and moved into place only when complete. Finished renditions are reused only when they are complete *and* were made with identical settings (source, bitrate, frame rate, preset, ffmpeg version are fingerprinted), so changing the ladder never silently reuses old output.

## Production checklist (Cloudflare)

Do these once per domain before real users play the video.

**1. Serve through your custom domain**, never `r2.dev` (rate-limited, uncached).

**2. Create a Cache Rule. This is required.** Cloudflare decides what to cache by file *extension*, not by `Content-Type`. Its default list includes `.mp4` but **not `.m3u8` or `.m4s`**, and the `Cache-Control` header this tool uploads does not change that. Without a rule, every playlist and segment request goes to R2 instead of the CDN edge.

- Dashboard: your domain, **Caching, Cache Rules, Create rule**.
- Rule name: anything, e.g. `HLS playlists and segments`.
- Edit the filter expression to: `(http.request.uri.path.extension in {"m3u8" "m4s"})`
- Cache eligibility: **Eligible for cache**. Leave Edge TTL and Browser TTL at their defaults, which respect the origin's `Cache-Control` header.
- Deploy.

Every uploaded file carries `Cache-Control: public, max-age=31536000, immutable`. That is safe because a version's URLs never change.

**3. Verify.** After the rule is live, the first request to a file should report `cf-cache-status: MISS` and later ones `HIT`:

```bash
curl -sI https://video.example.com/courses/week_1/lesson/v1/master.m3u8 | grep -i cf-cache-status
```

`r2_upload.py` also runs an advisory version of this check after every upload. Cache state is per data centre, so a missing `HIT` proves nothing, but `DYNAMIC` or `BYPASS` on a `.m3u8`/`.m4s` file means the rule is missing.

**4. CORS** is needed only if you also play the video in a browser from a different origin (for example Flutter web).

## Validation

`make_hls.py` validates every build automatically, and `r2_upload.py` validates again through your public URL after uploading. The validator checks:

- **Playlists:** `#EXT-X-MAP` present, `#EXT-X-ENDLIST`, correct target duration, no discontinuities, no missing, empty or orphan files.
- **Keyframes:** every segment starts on a keyframe, and boundaries match across renditions (so quality switches are seamless).
- **Master accuracy:** `BANDWIDTH`, `AVERAGE-BANDWIDTH`, `CODECS`, `RESOLUTION`, `FRAME-RATE` match the real files, and `CLOSED-CAPTIONS=NONE` is declared.
- **Audio/video continuity:** no timestamp gaps or overlaps, no cumulative drift, and an identical A/V offset across renditions.
- **Decode:** a full decode with zero errors plus random seeks.

Segments are checked *through the playlist*, because a bare `.m4s` has no initialization data and cannot be probed alone.

### Apple's `mediastreamvalidator` (optional, recommended before launch)

An independent compliance check from Apple. It is macOS only and sits behind an Apple Developer login:

1. Sign in at <https://developer.apple.com/download/all/> with any Apple ID.
2. Download **HTTP Live Streaming Tools, Validation** (the `Validation.dmg`, not Authoring) and run the installer.
3. Check it: `mediastreamvalidator -h`.
4. Run it against your published URL: `python3 validate_hls.py https://video.example.com/.../master.m3u8 --apple-validate`

The wrapper passes `--timeout 3600`. Apple's default 300-second cap stops after roughly 20% of an 18-minute video and then compares bitrates against partial data. One low-priority hint ("SHOULD declare subtitle or caption groups") is expected for videos without captions and can be ignored.

## Choosing bitrates

Before changing the ladder, compare candidates on your own footage:

```bash
python3 bitrate_trial.py "/path/to/video.mp4" -o bitrate_trial
open bitrate_trial/index.html
```

It encodes three excerpts (start, middle, and the most demanding stretch found by scanning the source's own packet sizes) at several bitrates using the production encoder settings, then reports VMAF and SSIM, the file size and projected 30-minute size, and saves the worst-scoring frame next to the source frame for each setting. The numbers inform the decision, but your own eyes on a real phone should make it.

## Playing the result

Point your player at the master URL. For Flutter's `video_player`, which plays HLS natively on Android (ExoPlayer) and iOS (AVPlayer) with no extra code for adaptive quality:

```dart
final controller = VideoPlayerController.networkUrl(
  Uri.parse('https://video.example.com/courses/week_1/lesson/v1/master.m3u8'),
);
await controller.initialize();
```

For a browser, use [hls.js](https://github.com/video-dev/hls.js) (Safari plays HLS natively).

## Project layout

| File | Purpose |
|---|---|
| `make_hls.py` | Main tool: probe, choose renditions, encode, write the master playlist, validate, optionally upload |
| `r2_upload.py` | Upload to R2: allowlist, ordering, immutability, resume, verification, advisory CDN check |
| `validate_hls.py` | Standalone validator for a local folder or a URL; optional Apple validator wrapper |
| `bitrate_trial.py` | Bitrate quality comparison harness |
| `hls_common.py` | Shared helpers: playlist parsing, RFC 8216 bandwidth math, codec string reading |
| `tests/` | Unit and end-to-end tests (`test_hls.py`, `test_upload_flow.py`) |
| `requirements.txt` | `boto3`, needed only for uploading |
| `compress_video.py`, `make_versions.py` | Legacy single-MP4 tools (see below) |

Generated folders (`hls_output/`, `bitrate_trial/`) and local state are git-ignored.

## Tests

```bash
python3 -m unittest discover tests
```

The suite covers the pure logic (ladder selection, frame-rate handling, RFC 8216 bandwidth math, versioning, resume fingerprints, upload allowlist and path validation) and an end-to-end upload flow against a fake S3 client and a local web server, so no real cloud account is needed. Some of it builds a tiny real HLS stream, so ffmpeg must be installed. You may see harmless `BrokenPipeError` lines from the local test web server; they are not failures.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Error: ffmpeg not found` | Install ffmpeg: `brew install ffmpeg` |
| `missing environment variable(s): R2_...` | Load your credentials: `set -a; . ~/.config/video-tool/r2.env; set +a` |
| `boto3 is required for uploads` | `pip3 install -r requirements.txt` |
| `--upload needs --remote-path` / `--path is required` | Choose the bucket folder explicitly, e.g. `--path courses/week_1/lesson` |
| `already published (master.m3u8 exists)` | By design. Published versions are immutable. Build a new version (omit `--version`) and upload that |
| `already holds N object(s) from an unfinished upload` | A previous upload was interrupted. Re-run `r2_upload.py ... --resume` |
| Cache check shows `DYNAMIC` | The Cloudflare Cache Rule is missing. See the [production checklist](#production-checklist-cloudflare) |
| `403` / `AccessDenied` when uploading | The API token lacks Object Read & Write on that bucket, or the account ID or bucket name is wrong |
| Apple validator: "Failed to fetch segment data", `-1009 ... offline` | Your own network dropped or the Mac went to sleep during the run (these are client-side errors, not server errors). Check the files with `curl -I`, then re-run. Prefixing the command with `caffeinate -i` keeps the Mac awake |
| Apple validator reports only ~20% of segments processed | It hit its runtime cap. `validate_hls.py --apple-validate` already lifts it; if you call the tool directly, pass `--timeout 3600` |
| Encode takes very long | 1080p two-pass `slow` is thorough. Use `--preset medium` or `fast`, or `--only 360p,720p` |
| `This ffmpeg build has no libvmaf filter` | Only `bitrate_trial.py` needs it. Install ffmpeg from Homebrew, which includes libvmaf |
| `.DS_Store` appears in an output folder | Harmless. The upload allowlist never sends it |

## Legacy single-file tools

These predate the HLS pipeline and still work, but new videos should use `make_hls.py`.

**`compress_video.py`** shrinks one file with manual codec, resolution and quality control:

```bash
python3 compress_video.py INPUT [-o OUTPUT] [--resolution 360p|720p|1080p|original|WxH] \
        [--codec h264|h265|vp9] [--crf 23 | --bitrate 1500k] [--preset medium] [--overwrite]
```

Aspect ratio and orientation are kept (a portrait video stays portrait). Size estimate: `MB ~ bitrate (kbps) x duration (s) / 8000`.

**`make_versions.py`** creates two standalone MP4s (`720p Standard` and `360p Data Saver`) for serving a plain file instead of a stream:

```bash
python3 make_versions.py INPUT [INPUT ...] [-d OUT_DIR] [--only standard|data_saver]
```
