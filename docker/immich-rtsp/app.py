#!/usr/bin/env python3
"""Pulls photos from Immich albums and serves them as a looping RTSP
slideshow via ffmpeg, for Projectivy Launcher's Media file wallpaper source
(Uri = rtsp://<this-host>:8554/wallpaper, Video toggle on).

Config via environment variables (see .env.example):
  IMMICH_SERVER_URL, IMMICH_API_KEY, IMMICH_ALBUM_NAMES (comma separated)
  PHOTO_COUNT (default 20), SECONDS_PER_IMAGE (default 15)
  REFRESH_INTERVAL_HOURS (default 24), RTSP_PORT (default 8554)
  WIDTH (default 1920), HEIGHT (default 1080)
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
PLAYLIST_PATH = DATA_DIR / "playlist.txt"

SERVER_URL = os.environ["IMMICH_SERVER_URL"]
API_KEY = os.environ["IMMICH_API_KEY"]
ALBUM_NAMES = [a.strip() for a in os.environ["IMMICH_ALBUM_NAMES"].split(",") if a.strip()]
PHOTO_COUNT = int(os.environ.get("PHOTO_COUNT", "20"))
SECONDS_PER_IMAGE = int(os.environ.get("SECONDS_PER_IMAGE", "15"))
REFRESH_INTERVAL_HOURS = float(os.environ.get("REFRESH_INTERVAL_HOURS", "24"))
RTSP_PORT = os.environ.get("RTSP_PORT", "8554")
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

    playlist_lines = []
    for img in sorted(IMAGES_DIR.glob("*.jpg")):
        playlist_lines.append(f"file '{img}'")
        playlist_lines.append(f"duration {SECONDS_PER_IMAGE}")
    playlist_lines.append(f"file '{sorted(IMAGES_DIR.glob('*.jpg'))[0]}'")
    PLAYLIST_PATH.write_text("\n".join(playlist_lines) + "\n", encoding="utf-8")

    log(f"Synced {len(written)} images, {total_mb:.1f} MB total.")
    return True


def start_ffmpeg():
    global ffmpeg_proc
    cmd = [
        "ffmpeg", "-y", "-loglevel", "warning",
        "-stream_loop", "-1",
        "-f", "concat", "-safe", "0", "-i", str(PLAYLIST_PATH),
        "-vf", f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
               f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2,format=yuv420p",
        "-r", "25",
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage",
        "-g", "50",
        "-rtsp_flags", "listen",
        "-listen_timeout", "-1",
        "-f", "rtsp", f"rtsp://0.0.0.0:{RTSP_PORT}/wallpaper",
    ]
    log(f"Starting ffmpeg: {' '.join(cmd)}")
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
    if not safe_sync_images() and not PLAYLIST_PATH.exists():
        sys.exit("Initial sync failed and no cached playlist exists. Exiting.")

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
