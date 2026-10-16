#!/usr/bin/env python3
"""Upload one built HLS version to Cloudflare R2 (S3 API), then verify it.

Usage:
    python3 r2_upload.py <out>/<slug>/v<N> --path exercises/trimester_2/my_video [--dry-run] [--resume]

Credentials come from environment variables only (never CLI args, never written to disk):
    R2_ACCOUNT_ID  R2_ACCESS_KEY_ID  R2_SECRET_ACCESS_KEY  R2_BUCKET
    R2_PUBLIC_BASE_URL   your custom domain, e.g. https://video.example.com  (remote validation + final URL)

Rules:
  * Files go to <path>/v<N>/ where --path is the bucket folder you choose (no default, no forced prefix).
  * A published version is immutable. If master.m3u8 already exists under <path>/v<N>/ the upload is refused.
  * master.m3u8 is uploaded LAST and acts as the commit marker. A version without it is "partial";
    --resume fills in missing or size-mismatched files.
  * Only an allowlist of files is ever uploaded (master, index.m3u8, init.mp4, seg_NNNNN.m4s).
"""

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import hls_common as common
from hls_common import human_size

REQUIRED_ENV = ["R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"]
WORKERS = 16
IMMUTABLE_CACHE = "public, max-age=31536000, immutable"   # every path is unique per version

ALLOWED_PATTERNS = [
    ("segment", re.compile(r"^[0-9]+p/seg_\d{5}\.m4s$")),
    ("init", re.compile(r"^[0-9]+p/init\.mp4$")),
    ("playlist", re.compile(r"^[0-9]+p/index\.m3u8$")),
    ("master", re.compile(r"^master\.m3u8$")),
]
UPLOAD_ORDER = {"segment": 0, "init": 0, "playlist": 1, "master": 2}


# --------------------------------------------------------------- pure logic

def classify(rel_path):
    """Return the allowlist category of a relative POSIX path, or None if it must not be uploaded."""
    for kind, pattern in ALLOWED_PATTERNS:
        if pattern.match(rel_path):
            return kind
    return None


def content_type(rel_path):
    if rel_path.endswith(".m3u8"):
        return "application/vnd.apple.mpegurl"
    if rel_path.endswith(".m4s"):
        return "video/iso.segment"
    if rel_path.endswith(".mp4"):
        return "video/mp4"
    raise ValueError(f"no content type for {rel_path}")


def collect_files(version_dir):
    """Return (allowed, excluded). allowed: [(rel, abs, kind)] in upload order; excluded: [rel]."""
    allowed, excluded = [], []
    for root, dirs, files in os.walk(version_dir):
        dirs.sort()
        for name in sorted(files):
            absolute = os.path.join(root, name)
            rel = os.path.relpath(absolute, version_dir).replace(os.sep, "/")
            kind = classify(rel)
            if kind and not os.path.islink(absolute):
                allowed.append((rel, absolute, kind))
            else:
                excluded.append(rel)
    allowed.sort(key=lambda item: (UPLOAD_ORDER[item[2]], item[0]))
    return allowed, excluded


PATH_PATTERN = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")


def normalize_base_path(path):
    """Validate the bucket folder, e.g. 'exercises/trimester_2/trimester_2_yoga' (slashes at the ends are trimmed)."""
    cleaned = (path or "").strip().strip("/")
    if not cleaned or not PATH_PATTERN.match(cleaned) or any(part in (".", "..") for part in cleaned.split("/")):
        raise ValueError(f"invalid bucket path {path!r}: use letters, digits, '.', '_' or '-' separated by '/'")
    return cleaned


def parse_version_prefixes(common_prefixes, base_path):
    """['a/b/v1/', 'a/b/v12/', 'a/b/other/'] with base 'a/b' -> [1, 12]"""
    pattern = re.compile(rf"^{re.escape(base_path)}/v(\d+)/$")
    return sorted(int(m.group(1)) for p in common_prefixes if (m := pattern.match(p)))


def key_for(base_path, version, rel):
    return f"{base_path}/v{version}/{rel}"


# ----------------------------------------------------------------- R2 client

def load_settings(env=None):
    env = os.environ if env is None else env
    missing = [name for name in REQUIRED_ENV if not env.get(name)]
    if missing:
        raise RuntimeError("missing environment variable(s): " + ", ".join(missing)
                           + ". Set them in your shell (see README, 'Cloudflare R2 setup').")
    return {
        "account_id": env["R2_ACCOUNT_ID"], "access_key": env["R2_ACCESS_KEY_ID"],
        "secret_key": env["R2_SECRET_ACCESS_KEY"], "bucket": env["R2_BUCKET"],
        "public_base": (env.get("R2_PUBLIC_BASE_URL") or "").rstrip("/"),
    }


def make_client(settings):
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        raise RuntimeError("boto3 is required for uploads: pip3 install -r requirements.txt")
    options = {"signature_version": "s3v4", "retries": {"max_attempts": 5, "mode": "adaptive"},
               "max_pool_connections": WORKERS * 2}
    try:   # newer botocore adds checksum headers that R2 does not need; only send them when required
        config = Config(request_checksum_calculation="when_required",
                        response_checksum_validation="when_required", **options)
    except TypeError:
        config = Config(**options)
    return boto3.client("s3", endpoint_url=f"https://{settings['account_id']}.r2.cloudflarestorage.com",
                        aws_access_key_id=settings["access_key"], aws_secret_access_key=settings["secret_key"],
                        region_name="auto", config=config)


def list_objects(client, bucket, prefix):
    sizes, token = {}, None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        page = client.list_objects_v2(**kwargs)
        for obj in page.get("Contents", []):
            sizes[obj["Key"]] = obj["Size"]
        if not page.get("IsTruncated"):
            return sizes
        token = page["NextContinuationToken"]


def remote_versions(client, bucket, base_path):
    prefixes, token = [], None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": f"{base_path}/", "Delimiter": "/"}
        if token:
            kwargs["ContinuationToken"] = token
        page = client.list_objects_v2(**kwargs)
        prefixes += [p["Prefix"] for p in page.get("CommonPrefixes", [])]
        if not page.get("IsTruncated"):
            return parse_version_prefixes(prefixes, base_path)
        token = page["NextContinuationToken"]


# -------------------------------------------------------------------- upload

def upload_group(client, settings, base_path, version, items, label):
    if not items:
        return
    started = time.time()
    done, total_bytes = 0, sum(os.path.getsize(path) for _, path, _ in items)

    def send(item):
        rel, path, _ = item
        client.upload_file(path, settings["bucket"], key_for(base_path, version, rel), ExtraArgs={
            "ContentType": content_type(rel), "CacheControl": IMMUTABLE_CACHE})
        return rel

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(send, item) for item in items]
        for future in as_completed(futures):
            future.result()          # raises on failure; caller aborts before master.m3u8
            done += 1
            if done % 50 == 0 or done == len(items):
                print(f"    {label}: {done}/{len(items)}", end="\r", flush=True)
    print(f"    {label}: {len(items)} files, {human_size(total_bytes)} in {time.time() - started:.0f}s      ")


def cdn_check(master_url, segment_url):
    """Advisory only: report what Cloudflare says about cacheability. Never raises, never fails a release."""
    import urllib.error
    import urllib.request

    def fetch(url, headers=None):
        request = urllib.request.Request(url, headers={"User-Agent": "hls-cdn-check/1", **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                response.read(1)
                return dict(response.headers), response.status
        except (urllib.error.URLError, OSError) as err:
            return {"error": str(err)}, None

    print("\n   CDN check (advisory; cache state is per data centre)")
    for label, url, extra in (("master.m3u8", master_url, None), ("segment", segment_url, None),
                              ("segment (Range)", segment_url, {"Range": "bytes=0-1023"})):
        seen, info = [], {}
        for attempt in range(3):
            headers, status = fetch(url, extra)
            if status is None:
                print(f"     {label}: request failed ({headers['error']})")
                break
            lower = {k.lower(): v for k, v in headers.items()}
            info = lower
            seen.append(lower.get("cf-cache-status", "(none)"))
            if seen[-1].upper() == "HIT":
                break
            time.sleep(1)
        if not seen:
            continue
        status_text = " -> ".join(seen)
        if "HIT" in (s.upper() for s in seen):
            verdict = "cached at the edge"
        elif any(s.upper() in ("DYNAMIC", "BYPASS") for s in seen):
            verdict = "NOT ELIGIBLE FOR CACHE: the Cloudflare Cache Rule is probably missing (see README)"
        elif seen == ["(none)"] * len(seen):
            verdict = "no cf-cache-status header (not served through Cloudflare?)"
        else:
            verdict = "eligible for cache; no HIT seen from this location yet (inconclusive)"
        print(f"     {label}: cf-cache-status {status_text}: {verdict}")
        print(f"       cache-control: {info.get('cache-control', '(none)')} | content-type: {info.get('content-type', '(none)')}")


def upload_version(client, settings, version_dir, base_path, version, dry_run=False, resume=False,
                   skip_validate=False):
    version_dir = os.path.abspath(version_dir)
    bucket = settings["bucket"]
    base_path = normalize_base_path(base_path)
    prefix = key_for(base_path, version, "")
    allowed, excluded = collect_files(version_dir)
    if not any(kind == "master" for _, _, kind in allowed):
        raise RuntimeError(f"{version_dir} has no master.m3u8; build it with make_hls.py first")

    if not skip_validate:
        import validate_hls
        report = validate_hls.validate_local(version_dir, deep_decode=False)
        if not report.ok:
            report.print()
            raise RuntimeError("local validation failed; refusing to upload a broken version")

    existing = {} if dry_run and client is None else list_objects(client, bucket, prefix)
    master_key = key_for(base_path, version, "master.m3u8")
    if master_key in existing:
        raise RuntimeError(f"{prefix} is already published (master.m3u8 exists). Published versions are "
                           "immutable: build a new version instead.")
    if existing and not resume:
        raise RuntimeError(f"{prefix} already holds {len(existing)} object(s) from an unfinished upload. "
                           "Re-run with --resume to complete it.")

    todo = [item for item in allowed
            if existing.get(key_for(base_path, version, item[0])) != os.path.getsize(item[1])]
    total = sum(os.path.getsize(path) for _, path, _ in allowed)
    print(f"\n   R2 upload: s3://{bucket}/{prefix}")
    print(f"     {len(allowed)} files ({human_size(total)}) allowed; {len(allowed) - len(todo)} already present; "
          f"{len(todo)} to upload")
    if excluded:
        print(f"     excluded from upload ({len(excluded)}): " + ", ".join(excluded[:8])
              + (" ..." if len(excluded) > 8 else ""))
    if dry_run:
        for rel, _, kind in todo[:6]:
            print(f"     would upload {key_for(base_path, version, rel)}  [{content_type(rel)}]")
        if len(todo) > 6:
            print(f"     ... and {len(todo) - 6} more (master.m3u8 last)")
        print("     dry run: nothing was uploaded")
        return None

    for order, label in ((0, "segments + init"), (1, "rendition playlists"), (2, "master.m3u8 (commit)")):
        upload_group(client, settings, base_path, version,
                     [item for item in todo if UPLOAD_ORDER[item[2]] == order], label)

    remote = list_objects(client, bucket, prefix)
    expected = {key_for(base_path, version, rel): os.path.getsize(path) for rel, path, _ in allowed}
    missing = [k for k in expected if k not in remote]
    mismatched = [k for k in expected if k in remote and remote[k] != expected[k]]
    extra = [k for k in remote if k not in expected]
    if missing or mismatched or extra:
        raise RuntimeError(f"post-upload verification failed: {len(missing)} missing, "
                           f"{len(mismatched)} size mismatches, {len(extra)} unexpected objects")
    print(f"     verified: {len(expected)} objects in R2 match local sizes")

    master_url = f"{settings['public_base']}/{prefix}master.m3u8" if settings["public_base"] else None
    ok = True
    if master_url:
        import validate_hls
        report = validate_hls.validate_remote(master_url, local_dir=version_dir)
        report.print()
        ok = report.ok
        first_segment = next((rel for rel, _, kind in allowed if kind == "segment"), None)
        if first_segment:
            cdn_check(master_url, f"{settings['public_base']}/{prefix}{first_segment}")
    else:
        print("     R2_PUBLIC_BASE_URL not set: skipped remote validation and the CDN check")

    work_receipt = os.path.join(os.path.dirname(version_dir), ".work", os.path.basename(version_dir),
                                "publish.json")
    try:
        receipt = json.load(open(work_receipt, encoding="utf-8")) if os.path.isfile(work_receipt) else {}
        receipt.update({"master_url": master_url, "bucket_prefix": prefix, "remote_validated": bool(master_url) and ok,
                        "uploaded": datetime.now(timezone.utc).isoformat()})
        os.makedirs(os.path.dirname(work_receipt), exist_ok=True)
        with open(work_receipt, "w", encoding="utf-8") as handle:
            json.dump(receipt, handle, indent=2)
    except (OSError, ValueError):
        pass

    print("\n   " + ("Published and validated." if (master_url and ok) else
                     "Uploaded, but remote validation did not pass." if master_url else "Uploaded."))
    if master_url:
        print(f"   Master URL: {master_url}")
    if master_url and ok:
        print("   Next: point your database at this URL. Keep the previous version for a grace period before deleting it.")
    elif master_url:
        print("   Do NOT switch your database to this URL. Fix the cause and publish a new version.")
        raise RuntimeError("remote validation failed")
    return master_url


def parse_args():
    parser = argparse.ArgumentParser(description="Upload a built HLS version folder to Cloudflare R2.")
    parser.add_argument("version_dir", help="Local folder like <out>/<slug>/v<N>")
    parser.add_argument("--path", required=True,
                        help="Bucket folder to publish into, e.g. exercises/trimester_2/trimester_2_yoga "
                             "(the version folder v<N> is added automatically)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be uploaded; touch nothing")
    parser.add_argument("--resume", action="store_true", help="Finish a partially uploaded version")
    parser.add_argument("--skip-validate", action="store_true", help="Skip the local validation before uploading")
    return parser.parse_args()


def main():
    args = parse_args()
    version_dir = os.path.abspath(args.version_dir.rstrip("/"))
    match = re.fullmatch(r"v(\d+)", os.path.basename(version_dir))
    if not match or not os.path.isdir(version_dir):
        sys.exit("Error: version_dir must be an existing folder named v<N>, e.g. out/my-video/v1")
    version = int(match.group(1))
    try:
        settings = load_settings()
        client = make_client(settings)
        upload_version(client, settings, version_dir, args.path, version, dry_run=args.dry_run,
                       resume=args.resume, skip_validate=args.skip_validate)
    except (RuntimeError, ValueError) as err:
        sys.exit(f"Error: {err}")


if __name__ == "__main__":
    main()
