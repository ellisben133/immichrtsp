#!/usr/bin/env python3
"""Pulls photos from Immich albums and publishes them as a looping RTSP
slideshow to a MediaMTX server (the 'mediamtx' service in the same compose
stack), for Projectivy Launcher's Media file wallpaper source
(Uri = rtsp://<this-host>:8554/wallpaper, Video toggle on).

ffmpeg's own built-in RTSP "listen" server mode turned out to be
unreliable in testing (it attempted an outbound connect() instead of
actually binding/listening, failing instantly with "Connection refused"
on every run regardless of -listen_timeout). Publishing to MediaMTX --
purpose-built, actively maintained RTSP server software -- as a normal
client is a far more standard, well-documented pattern.

Config via environment variables (see .env.example):
  IMMICH_SERVER_URL, IMMICH_API_KEY, IMMICH_ALBUM_NAMES (comma separated)
  PHOTO_COUNT (default 20), SECONDS_PER_IMAGE (default 15)
  FADE_SECONDS (default 1.5, crossfade duration between photos)
  REFRESH_INTERVAL_HOURS (default 24), RTSP_PUBLISH_URL
  (default rtsp://mediamtx:8554/wallpaper)
  WIDTH (default 1920), HEIGHT (default 1080)

Two-stage design (changed after an earlier version OOM'd a 2 CPU / 4GB
shared host): a *render* step builds the crossfaded slideshow into an
actual video file, combining photos two at a time in a chain (never more
than 2 real-time-ish inputs open at once, regardless of how many photos
are configured -- bounded resource use independent of PHOTO_COUNT). A
separate, much cheaper *stream* step just loops that finished file to
mediamtx with -c copy (no decode/encode/filtering at all, tiny constant
footprint). The first version instead kept every photo as a simultaneous
live input for the entire streaming lifetime -- correct output, but a
completely different and much heavier resource profile that wasn't caught
by local testing with only 3 images.
"""
import json
import os
import random
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DATA_DIR = Path("/data")
IMAGES_DIR = DATA_DIR / "images"
SLIDESHOW_PATH = DATA_DIR / "slideshow.mp4"

SERVER_URL = os.environ["IMMICH_SERVER_URL"]
API_KEY = os.environ["IMMICH_API_KEY"]
ALBUM_NAMES = [a.strip() for a in os.environ["IMMICH_ALBUM_NAMES"].split(",") if a.strip()]
PHOTO_COUNT = int(os.environ.get("PHOTO_COUNT", "20"))
LANDSCAPE_ONLY = os.environ.get("LANDSCAPE_ONLY", "true").strip().lower() not in ("false", "0", "no", "")
SECONDS_PER_IMAGE = float(os.environ.get("SECONDS_PER_IMAGE", "15"))
FADE_SECONDS = float(os.environ.get("FADE_SECONDS", "1.5"))
REFRESH_INTERVAL_HOURS = float(os.environ.get("REFRESH_INTERVAL_HOURS", "24"))
RTSP_PUBLISH_URL = os.environ.get("RTSP_PUBLISH_URL", "rtsp://mediamtx:8554/wallpaper")
WIDTH = os.environ.get("WIDTH", "1920")
HEIGHT = os.environ.get("HEIGHT", "1080")

ffmpeg_proc = None
shutting_down = False


def log(msg):
    print(f"[immich-rtsp] {msg}", flush=True)


def api_post(path, body):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        SERVER_URL.rstrip("/") + path, data=data, method="POST",
        headers={"x-api-key": API_KEY, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def api_get(path):
    req = urllib.request.Request(SERVER_URL.rstrip("/") + path, headers={"x-api-key": API_KEY})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def api_get_binary(path):
    req = urllib.request.Request(SERVER_URL.rstrip("/") + path, headers={"x-api-key": API_KEY})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def get_album_assets(album_id):
    assets = []
    page = 1
    while True:
        result = api_post("/api/search/metadata", {"albumIds": [album_id], "size": 200, "page": page})
        items = result["assets"]["items"]
        assets.extend(items)
        next_page = result["assets"].get("nextPage")
        if not next_page or not items:
            break
        page = int(next_page)
    return assets


def sync_images():
    """Downloads a fresh batch of previews and atomically swaps the images dir."""
    log(f"Syncing from albums: {', '.join(ALBUM_NAMES)}")
    albums = api_get("/api/albums")
    wanted = {a["albumName"]: a for a in albums if a["albumName"] in ALBUM_NAMES}
    missing = set(ALBUM_NAMES) - set(wanted)
    if missing:
        log(f"WARNING: album(s) not found: {', '.join(missing)}")

    pool = []
    for name, album in wanted.items():
        assets = get_album_assets(album["id"])
        log(f"  {name}: {len(assets)} photos")
        pool.extend(assets)

    if LANDSCAPE_ONLY:
        before = len(pool)
        pool = [a for a in pool if a.get("width", 0) >= a.get("height", 0)]
        log(f"  landscape filter: {len(pool)} of {before} photos kept")

    if not pool:
        log("No photos found this sync, keeping existing set.")
        return False

    random.shuffle(pool)
    selected = pool[:PHOTO_COUNT]

    tmp_dir = DATA_DIR / "images_new"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    written = []
    for i, asset in enumerate(selected):
        try:
            data = api_get_binary(f"/api/assets/{asset['id']}/thumbnail?size=preview")
        except urllib.error.HTTPError as e:
            log(f"  skip {asset['id']}: HTTP {e.code}")
            continue
        out = tmp_dir / f"img_{i:03d}.jpg"
        out.write_bytes(data)
        written.append(out)

    if not written:
        log("Downloaded 0 images, keeping existing set.")
        shutil.rmtree(tmp_dir)
        return False

    total_mb = sum(f.stat().st_size for f in written) / 1024 / 1024

    if IMAGES_DIR.exists():
        shutil.rmtree(IMAGES_DIR)
    tmp_dir.rename(IMAGES_DIR)

    log(f"Synced {len(written)} images, {total_mb:.1f} MB total.")
    render_slideshow(sorted(IMAGES_DIR.glob("*.jpg")))
    return True


def _scale_pad_expr():
    return (
        f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2,format=yuv420p,setsar=1"
    )


def _scale_pad_filter(input_label, output_label):
    return f"[{input_label}]{_scale_pad_expr()}[{output_label}]"


def _run_ffmpeg_render(cmd, out_path):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not out_path.exists():
        log(f"render step failed (code {result.returncode}):")
        log(result.stderr[-2000:])
        raise RuntimeError("ffmpeg render step failed")


def render_slideshow(images):
    """Builds the crossfaded slideshow as an actual file, combining photos
    two at a time in a chain -- at most 2 real inputs open in any single
    ffmpeg call, regardless of how many photos there are. Runs once per
    sync (REFRESH_INTERVAL_HOURS), not continuously, so a brief CPU/memory
    cost here is fine even on a small shared host."""
    render_dir = DATA_DIR / "render"
    if render_dir.exists():
        shutil.rmtree(render_dir)
    render_dir.mkdir(parents=True, exist_ok=True)

    hold_tail = SECONDS_PER_IMAGE + FADE_SECONDS

    if len(images) == 1:
        cmd = [
            "ffmpeg", "-y", "-loglevel", "warning",
            "-loop", "1", "-t", str(SECONDS_PER_IMAGE), "-i", str(images[0]),
            "-vf", _scale_pad_expr(),
            "-r", "25", "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage",
            str(render_dir / "final.mp4"),
        ]
        log("Rendering single-image slideshow...")
        _run_ffmpeg_render(cmd, render_dir / "final.mp4")
    else:
        prev_file = render_dir / "step000.mp4"
        cmd = [
            "ffmpeg", "-y", "-loglevel", "warning",
            "-loop", "1", "-t", str(hold_tail), "-i", str(images[0]),
            "-vf", _scale_pad_expr(),
            "-r", "25", "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage",
            str(prev_file),
        ]
        log(f"Rendering slideshow, step 1/{len(images)}...")
        _run_ffmpeg_render(cmd, prev_file)
        prev_duration = hold_tail

        for i in range(1, len(images)):
            out_file = render_dir / f"step{i:03d}.mp4"
            offset = prev_duration - FADE_SECONDS
            cmd = [
                "ffmpeg", "-y", "-loglevel", "warning",
                "-i", str(prev_file),
                "-loop", "1", "-t", str(hold_tail), "-i", str(images[i]),
                "-filter_complex",
                # xfade requires both inputs to share a timebase; the
                # already-encoded prev_file and a fresh -loop 1 image
                # source don't by default (found by actually running
                # this, not by inspection -- "First input link main
                # timebase do not match the corresponding second input
                # link xfade timebase"). fps=25 on both normalizes it.
                f"[0:v]fps=25[vprev];" + _scale_pad_filter("1:v", "vnew") +
                f";[vprev][vnew]xfade=transition=fade:duration={FADE_SECONDS}:offset={offset}[vout]",
                "-map", "[vout]",
                "-r", "25", "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage",
                str(out_file),
            ]
            log(f"Rendering slideshow, step {i + 1}/{len(images)}...")
            _run_ffmpeg_render(cmd, out_file)
            prev_file.unlink(missing_ok=True)
            prev_file = out_file
            prev_duration = prev_duration + SECONDS_PER_IMAGE

        prev_file.rename(render_dir / "final.mp4")

    tmp_path = DATA_DIR / "slideshow.mp4.new"
    shutil.move(str(render_dir / "final.mp4"), str(tmp_path))
    shutil.rmtree(render_dir, ignore_errors=True)
    tmp_path.replace(SLIDESHOW_PATH)
    log(f"Slideshow rendered: {SLIDESHOW_PATH} ({SLIDESHOW_PATH.stat().st_size / 1024 / 1024:.1f} MB)")


def start_ffmpeg():
    global ffmpeg_proc
    # Just loop the pre-rendered file -- -c copy means no decode, encode,
    # or filtering at all, trivial CPU/memory, regardless of how many
    # photos are in it or how long it runs for.
    cmd = [
        "ffmpeg", "-y", "-loglevel", "warning",
        "-re", "-stream_loop", "-1", "-i", str(SLIDESHOW_PATH),
        "-c", "copy",
        "-rtsp_transport", "tcp", "-f", "rtsp", RTSP_PUBLISH_URL,
    ]
    log(f"Starting ffmpeg (streaming {SLIDESHOW_PATH}): {' '.join(cmd)}")
    # stderr/stdout inherit the parent's (this process's) streams, so
    # ffmpeg's actual errors land directly in `docker logs` instead of
    # being swallowed in an unread pipe buffer.
    ffmpeg_proc = subprocess.Popen(cmd)


def stop_ffmpeg():
    global ffmpeg_proc
    if ffmpeg_proc and ffmpeg_proc.poll() is None:
        ffmpeg_proc.terminate()
        try:
            ffmpeg_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            ffmpeg_proc.kill()
    ffmpeg_proc = None


def handle_signal(signum, frame):
    global shutting_down
    shutting_down = True
    log(f"Received signal {signum}, shutting down...")
    stop_ffmpeg()
    sys.exit(0)


def safe_sync_images():
    """sync_images() wrapped so a transient Immich/network error logs and
    returns False instead of killing the whole service."""
    try:
        return sync_images()
    except Exception:
        import traceback
        log("Sync failed with an exception:")
        traceback.print_exc()
        return False


def main():
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not safe_sync_images() and not SLIDESHOW_PATH.exists():
        sys.exit("Initial sync failed and no cached slideshow exists. Exiting.")

    start_ffmpeg()
    last_sync = time.monotonic()
    refresh_seconds = REFRESH_INTERVAL_HOURS * 3600

    while not shutting_down:
        time.sleep(5)
        if ffmpeg_proc.poll() is not None:
            log(f"ffmpeg exited (code {ffmpeg_proc.returncode}), restarting in 2s...")
            time.sleep(2)
            start_ffmpeg()
            continue
        if time.monotonic() - last_sync >= refresh_seconds:
            if safe_sync_images():
                log("Restarting ffmpeg with refreshed images...")
                stop_ffmpeg()
                start_ffmpeg()
            last_sync = time.monotonic()


if __name__ == "__main__":
    main()
