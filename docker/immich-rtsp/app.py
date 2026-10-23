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
  SYNC_HOUR / SYNC_MINUTE (default 2 / 0, daily local-time resync),
  TZ (e.g. Australia/Sydney -- what "local time" means for SYNC_HOUR)
  RTSP_PUBLISH_URL (default rtsp://mediamtx:8554/wallpaper)
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
import datetime
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

# Make datetime.now() respect the TZ env var (set in docker-compose.yml) --
# without this, glibc may not pick up a TZ change made after the process's
# own startup environment was read.
if hasattr(time, "tzset"):
    time.tzset()

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
SYNC_HOUR = int(os.environ.get("SYNC_HOUR", "2"))
SYNC_MINUTE = int(os.environ.get("SYNC_MINUTE", "0"))
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


def _run_ffmpeg_render(cmd, out_path):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not out_path.exists():
        log(f"render step failed (code {result.returncode}):")
        log(result.stderr[-2000:])
        raise RuntimeError("ffmpeg render step failed")


def _prescale_image(src, dst):
    """Scale/pad an image to WxH exactly once. Used so the per-frame hot
    path (each image gets decoded ~375+ times as a -loop 1 source) only
    ever does a cheap format conversion, not a full resize -- found via
    local profiling that the resize filter, not the encoder preset, was
    the dominant cost: removing it was roughly as big a win as switching
    ultrafast<->veryfast, and the two stack additively."""
    cmd = [
        "ffmpeg", "-y", "-loglevel", "warning",
        "-i", str(src),
        "-vf", _scale_pad_expr(),
        "-frames:v", "1",
        str(dst),
    ]
    _run_ffmpeg_render(cmd, dst)


def render_slideshow(images):
    """Builds the crossfaded slideshow as an actual file.

    Earlier version chained crossfades by re-reading and re-encoding the
    *entire accumulated slideshow so far* at every step -- bounded to 2
    concurrent inputs (so it didn't repeat the original OOM), but total
    work still grew with roughly the square of the photo count, since
    step i re-encoded ~i photos worth of video just to add one more. On
    a 2 CPU / 4GB shared host that meant later steps took increasingly
    long and pushed memory into swap, discovered only once the render
    was running against 20 real photos -- the 6-image local test never
    ran long enough to show it.

    This version makes every step's cost constant, independent of N:
    each photo's "handoff" tail is regenerated fresh from its own raw
    JPEG (cheap -- a few seconds of re-decoding one image, not
    re-encoding the growing whole), crossfaded against the next photo
    into a small standalone segment, and every segment is stitched
    together at the end with a lossless -c copy concat (no re-encoding
    of anything already finished)."""
    render_dir = DATA_DIR / "render"
    if render_dir.exists():
        shutil.rmtree(render_dir)
    render_dir.mkdir(parents=True, exist_ok=True)

    prescaled_dir = render_dir / "prescaled"
    prescaled_dir.mkdir(parents=True, exist_ok=True)
    prescaled = []
    for i, img in enumerate(images):
        dst = prescaled_dir / f"pre{i:03d}.jpg"
        _prescale_image(img, dst)
        prescaled.append(dst)

    if len(prescaled) == 1:
        cmd = [
            "ffmpeg", "-y", "-loglevel", "warning",
            "-loop", "1", "-t", str(SECONDS_PER_IMAGE), "-i", str(prescaled[0]),
            "-vf", "format=yuv420p,setsar=1",
            "-r", "25", "-c:v", "libx264", "-preset", "ultrafast", "-tune", "stillimage",
            "-x264-params", "slice-max-size=1400",
            str(render_dir / "final.mp4"),
        ]
        log("Rendering single-image slideshow...")
        _run_ffmpeg_render(cmd, render_dir / "final.mp4")
    else:
        segments = []

        seg0 = render_dir / "seg000.mp4"
        cmd = [
            "ffmpeg", "-y", "-loglevel", "warning",
            "-loop", "1", "-t", str(SECONDS_PER_IMAGE), "-i", str(prescaled[0]),
            "-vf", "format=yuv420p,setsar=1",
            "-r", "25", "-c:v", "libx264", "-preset", "ultrafast", "-tune", "stillimage",
            "-x264-params", "slice-max-size=1400",
            str(seg0),
        ]
        log(f"Rendering slideshow, segment 1/{len(prescaled)}...")
        _run_ffmpeg_render(cmd, seg0)
        segments.append(seg0)

        for i in range(1, len(prescaled)):
            seg_path = render_dir / f"seg{i:03d}.mp4"
            cmd = [
                "ffmpeg", "-y", "-loglevel", "warning",
                "-loop", "1", "-t", str(FADE_SECONDS), "-i", str(prescaled[i - 1]),
                "-loop", "1", "-t", str(SECONDS_PER_IMAGE), "-i", str(prescaled[i]),
                "-filter_complex",
                f"[0:v]format=yuv420p,setsar=1,fps=25[vtail];"
                f"[1:v]format=yuv420p,setsar=1,fps=25[vnew];"
                f"[vtail][vnew]xfade=transition=fade:duration={FADE_SECONDS}:offset=0[vout]",
                "-map", "[vout]",
                "-r", "25", "-c:v", "libx264", "-preset", "ultrafast", "-tune", "stillimage",
                "-x264-params", "slice-max-size=1400",
                str(seg_path),
            ]
            log(f"Rendering slideshow, segment {i + 1}/{len(prescaled)}...")
            _run_ffmpeg_render(cmd, seg_path)
            segments.append(seg_path)

        concat_list = render_dir / "concat.txt"
        concat_list.write_text(
            "\n".join(f"file '{s.resolve().as_posix()}'" for s in segments) + "\n",
            encoding="utf-8",
        )
        final_path = render_dir / "final.mp4"
        cmd = [
            "ffmpeg", "-y", "-loglevel", "warning",
            "-f", "concat", "-safe", "0", "-i", str(concat_list),
            "-c", "copy", str(final_path),
        ]
        log("Stitching segments together...")
        _run_ffmpeg_render(cmd, final_path)
        for s in segments:
            s.unlink(missing_ok=True)

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


def next_sync_at(after):
    """Next datetime at SYNC_HOUR:SYNC_MINUTE (local time), strictly after
    `after` -- today if that time hasn't passed yet, tomorrow otherwise."""
    candidate = after.replace(hour=SYNC_HOUR, minute=SYNC_MINUTE, second=0, microsecond=0)
    if candidate <= after:
        candidate += datetime.timedelta(days=1)
    return candidate


def main():
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not safe_sync_images() and not SLIDESHOW_PATH.exists():
        sys.exit("Initial sync failed and no cached slideshow exists. Exiting.")

    start_ffmpeg()
    next_sync = next_sync_at(datetime.datetime.now())
    log(f"Next scheduled sync: {next_sync.isoformat()} ({time.tzname[0] if hasattr(time, 'tzname') else 'local'})")

    while not shutting_down:
        time.sleep(5)
        if ffmpeg_proc.poll() is not None:
            log(f"ffmpeg exited (code {ffmpeg_proc.returncode}), restarting in 2s...")
            time.sleep(2)
            start_ffmpeg()
            continue
        now = datetime.datetime.now()
        if now >= next_sync:
            if safe_sync_images():
                log("Restarting ffmpeg with refreshed images...")
                stop_ffmpeg()
                start_ffmpeg()
            next_sync = next_sync_at(now)
            log(f"Next scheduled sync: {next_sync.isoformat()}")


if __name__ == "__main__":
    main()
