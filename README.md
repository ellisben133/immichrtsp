# immich-rtsp

Serves a looping RTSP slideshow of photos pulled from [Immich](https://immich.app/)
albums, for use as a TV launcher's live background -- specifically built
for [Projectivy Launcher](https://github.com/prjctvy/projectivy-launcher)
on Android TV (Media file source, `rtsp://` Uri, Video toggle on), but the
stream itself is plain RTSP and works with anything that can play one.

No file pushing to the TV, no cron+adb pipeline, just one stream URL that's
always live.

Two containers:
- **mediamtx** -- the actual RTSP server (official `bluenviron/mediamtx`
  image, zero custom code, pulled straight from Docker Hub). Exposes 8554.
- **immich-rtsp** -- a small Python+ffmpeg publisher: pulls photos from
  Immich, *renders* a crossfaded slideshow to a video file, then
  *streams* that file to mediamtx over the internal Docker network as a
  normal RTSP client.

(First attempt used ffmpeg's own built-in RTSP "listen" server mode
instead of mediamtx, to avoid a second container -- it turned out
unreliable: it attempted an outbound `connect()` instead of actually
binding/listening, failing instantly on every run. Publishing to a real
RTSP server is the standard, well-documented pattern, hence the second
container.)

**A second incident worth knowing about:** the crossfade feature's first
version kept every configured photo open as a simultaneous real-time ffmpeg
input for as long as the stream ran, rather than briefly during a render --
correct output, but a fundamentally different and much heavier resource
profile than local testing (done with only 3 images) revealed. At
`PHOTO_COUNT=20` on a 2 CPU / 4GB host shared with other services, it drove
memory to 98% and swap to 99.98%, hanging `docker stop`/`docker kill` and
requiring a force-stop at the Proxmox host level to recover. The service
now renders the crossfaded slideshow to a file first (combining photos two
at a time in a chain -- never more than 2 real inputs open regardless of
`PHOTO_COUNT`), and streams that finished file with `-c copy` (no decode,
encode, or filtering at all during steady-state streaming). Both containers
also now carry hard `mem_limit`/`cpus` caps in `docker-compose.yml`, so a
future bug fails loudly (the container gets OOM-killed, visible in its own
logs) instead of starving the host.

**A third thing, caught deploying the fix above rather than beforehand:**
that first "render" design still chained crossfades by re-reading and
re-encoding the *entire accumulated slideshow so far* at every step --
bounded to 2 concurrent inputs (so no repeat of the OOM), but total work
still grew with roughly the square of the photo count, since step *i*
re-encoded ~*i* photos worth of video just to add one more. Caught live:
swap climbed from near-zero to 27% while still only on step 2 of 20, and
each step was visibly taking longer than the last. The current version
regenerates each photo's crossfade "handoff" fresh from its own raw JPEG
(cheap, a few seconds of re-decoding one image) rather than from the
growing rendered file, then stitches every segment together losslessly at
the end -- constant cost per step regardless of position in the sequence.
Verified locally before shipping again: 10 full-1920x1080 images at real
`SECONDS_PER_IMAGE=15`/`FADE_SECONDS=1.5` settings rendered in 64s total
with per-segment timing staying flat (4.3-7.3s each, no growth trend), and
the stitched output decoded start-to-finish with zero errors.

**Verified working end to end** (2026-10-23): confirmed live on the actual
TV, crossfading through real photos from both configured albums, via
Projectivy's Media file source pointed at the stream -- across all three
iterations of this feature above.

## 1. Copy the `docker/immich-rtsp/` folder to your server

`Dockerfile`, `app.py`, `docker-compose.yml`, `.env.example` -- keep them
together, in that same relative layout (`docker/immich-rtsp/`) if you're
deploying straight from a clone of this repo, since the Portainer stack
setup below (step 4) references that path.

## 2. Create your `.env`

```
cd docker/immich-rtsp
cp .env.example .env
```

Edit it: at minimum set `IMMICH_SERVER_URL` and `IMMICH_API_KEY` (an
Immich API key: Immich web UI -> Account Settings -> API Keys). Defaults:
20 photos, landscape-only, 15 seconds each with a 1.5s crossfade, refreshed
every 24h.

Config reference:
- `IMMICH_ALBUM_NAMES` -- comma-separated, exact album names to pull from
- `PHOTO_COUNT` -- how many photos to keep per sync (default 20)
- `LANDSCAPE_ONLY` -- `true`/`false` (default `true`). Portrait photos get
  letterboxed with black bars on the sides rather than filling the screen,
  so this filters them out using Immich's reported width/height.
- `SECONDS_PER_IMAGE` -- how long each photo is held before crossfading to
  the next (default 15). Change takes effect on the next ffmpeg restart
  (next sync, or just restart the `immich-rtsp` container -- no rebuild
  needed, it's an env var, not a code change)
- `FADE_SECONDS` -- crossfade duration between photos (default 1.5)
- `REFRESH_INTERVAL_HOURS` -- how often to pull a fresh set from Immich,
  restarting ffmpeg (brief reconnect blip on the TV) each time (default 24)

Each sync *renders* the crossfaded slideshow to a file first: each photo
becomes its own small, constant-cost segment (its handoff crossfade
regenerated fresh from the raw JPEG, not from any growing accumulated
file), and all segments are stitched together losslessly at the end. Then
it restarts the lightweight `-c copy` stream that loops that file to
mediamtx. See the two resource-usage notes above for why it's built this
way and not as one continuous multi-input ffmpeg process (or a naive
chain that re-encodes everything so far at every step).

## 3. Build the immich-rtsp image once via terminal

Only `immich-rtsp` needs a local build -- `mediamtx` is a public image
Portainer can pull directly with no builder involved. The compose file
references a pre-built local image (`immich-rtsp:latest`) rather than
building inline, since Portainer's own build pipeline (BuildKit) turned out
broken in this setup (`listing workers for Build` / `http2: frame too
large` errors):

```
git clone https://github.com/ellisben133/immichrtsp.git
cd immichrtsp/docker/immich-rtsp
DOCKER_BUILDKIT=0 docker build -t immich-rtsp:latest .
```

(`DOCKER_BUILDKIT=0` uses Docker's classic builder, sidestepping the same
buildx/BuildKit path that failed in Portainer.) Re-run this build command
any time `app.py`/`Dockerfile` changes (e.g. after a `git pull`).

## 4. Deploy as a Portainer stack

Portainer -> **Stacks** -> **Add stack** -> **Repository**, URL
`https://github.com/ellisben133/immichrtsp.git`, ref `refs/heads/main`,
compose path `docker/immich-rtsp/docker-compose.yml`. Paste your `.env`
contents into the stack's environment variables. **Deploy the stack** --
`mediamtx` gets pulled fresh, `immich-rtsp` just starts the image you
already built.

If you update this repo later, use the stack's **Pull and redeploy**
button -- just remember to re-run the terminal build step first if
`app.py`/`Dockerfile` changed, otherwise it'll redeploy the old image.

## 5. Verify the stream actually works before touching Projectivy

Check `immich-rtsp`'s logs first (Portainer or `docker logs immich-rtsp`)
-- you should see it list album photo counts, "Synced N images", several
"Rendering slideshow, step i/N..." lines, "Slideshow rendered: ...", then
"Starting ffmpeg (streaming ...)" with no repeating crash/restart after
that. Also worth a glance at the container's CPU/memory in Portainer
during that render step the first time, to see it settle back down
afterward (see the resource-usage note above).

Then test the RTSP feed itself from any machine on your network:
```
ffplay rtsp://<server-ip>:8554/wallpaper
```
or open that same URL in VLC (Media -> Open Network Stream). You should see
your photos crossfading. If it doesn't connect: check both containers are
running (`docker ps`), check port 8554 isn't blocked by a firewall on the
server, and check `immich-rtsp`'s logs for an ffmpeg error (it should
mention actually connecting/publishing to mediamtx, not a "Connection
refused").

## 6. Point Projectivy at it

On the TV, in Projectivy: Settings -> Appearance -> Wallpaper -> Launcher
wallpaper -> **Media file** -> Configure -> set **Uri** to
`rtsp://<server-ip>:8554/wallpaper` and toggle **Video** on.

## Notes

- mediamtx handles multiple simultaneous viewers fine (not that you need
  more than the TV), and is a real, independently maintained project --
  nothing about it needs maintaining on your end beyond pulling image
  updates occasionally.
- Images refresh every `REFRESH_INTERVAL_HOURS` (default 24h); ffmpeg
  restarts at that point, so the TV will show a brief black frame / reconnect
  blip once a day. Not noticeable for a background.
- Photos are Immich's "preview" size (not full originals), ~10-15MB per
  sync -- living on your server's disk, not the TV's.
- If the container restarts, it reuses whatever's cached in `/data` (a
  named Docker volume) until the next scheduled refresh, so a quick
  restart doesn't hammer your Immich server.

## What else is in this repo

This project grew out of a broader Android TV debloat job (disabling
manufacturer bloat/telemetry on a Sony Android TV, exploring launcher
options before settling on using Projectivy with this background service).
That work -- `tools/`, `batches/`, `docs/IMMICH_SYNC_SERVER_SETUP.md` (an
alternative adb-push-based approach to the same background-photos goal,
superseded by the RTSP service above but kept as a documented fallback) --
is still here but not what this repo is actively maintained for. See
[docs/ANDROID_TV_DEBLOAT.md](docs/ANDROID_TV_DEBLOAT.md) for that side of
things.
