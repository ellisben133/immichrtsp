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

Each sync renders the crossfaded slideshow to a file first (each photo
becomes its own small segment, crossfaded against the next, then all
segments are stitched together losslessly) before streaming it with
`-c copy` -- no decode, encode, or filtering at all during steady-state
streaming, so CPU/memory stay minimal except briefly during a render.
Segments encode with libx264's `ultrafast` preset to keep render time
low on modest hardware.

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

Edit it: set `IMMICH_SERVER_URL`, `IMMICH_API_KEY` (an Immich API key:
Immich web UI -> Account Settings -> API Keys), and `IMMICH_ALBUM_NAMES`.
Defaults: 20 photos, landscape-only, 15 seconds each with a 1.5s
crossfade, refreshed daily at 2am local time.

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
- `SYNC_HOUR` / `SYNC_MINUTE` -- local-time daily resync (default 2:00am),
  restarting ffmpeg (brief reconnect blip on the TV) once a day at that
  time rather than N hours after the container last started
- `TZ` -- what "local time" means for `SYNC_HOUR` (default
  `Australia/Sydney`), e.g. `TZ=America/New_York`. Without this the
  container runs on UTC, so 2am would actually fire at whatever UTC+0
  looks like for you

## 3. Build the immich-rtsp image once via terminal

Only `immich-rtsp` needs a local build -- `mediamtx` is a public image
Portainer can pull directly with no builder involved. The compose file
references a pre-built local image (`immich-rtsp:latest`) rather than
building inline, since Portainer's own build pipeline (BuildKit) can be
unreliable in some setups:

```
git clone https://github.com/ellisben133/immichrtsp.git
cd immichrtsp/docker/immich-rtsp
DOCKER_BUILDKIT=0 docker build -t immich-rtsp:latest .
```

Re-run this build command any time `app.py`/`Dockerfile` changes (e.g.
after a `git pull`).

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
that.

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
- Images refresh daily at `SYNC_HOUR`:`SYNC_MINUTE`; ffmpeg restarts at
  that point, so the TV will show a brief black frame / reconnect blip
  once a day. Not noticeable for a background.
- Photos are Immich's "preview" size (not full originals), living on your
  server's disk, not the TV's.
- If the container restarts, it reuses whatever's cached in `/data` (a
  named Docker volume) until the next scheduled refresh, so a quick
  restart doesn't hammer your Immich server.
- Both containers carry hard `mem_limit`/`cpus` caps in
  `docker-compose.yml`, so a bug fails loudly (the container gets
  OOM-killed, visible in its own logs) instead of starving the host.
