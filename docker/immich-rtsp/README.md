# immich-rtsp

Serves a looping RTSP slideshow of photos pulled from Immich albums, for use
as Projectivy Launcher's background (Media file source, `rtsp://` Uri,
Video toggle on) -- no file pushing to the TV, no cron+adb pipeline, just
one stream URL that's always live.

Two containers:
- **mediamtx** -- the actual RTSP server (official `bluenviron/mediamtx`
  image, zero custom code, pulled straight from Docker Hub). Exposes 8554.
- **immich-rtsp** -- a small Python+ffmpeg publisher: pulls photos from
  Immich, builds a looping slideshow, and *publishes* it to mediamtx over
  the internal Docker network as a normal RTSP client.

(First attempt used ffmpeg's own built-in RTSP "listen" server mode
instead of mediamtx, to avoid a second container -- it turned out
unreliable: it attempted an outbound `connect()` instead of actually
binding/listening, failing instantly on every run. Publishing to a real
RTSP server is the standard, well-documented pattern, hence the second
container.)

**Tested/untested split, honestly:** the Immich-fetching code is the same
logic already verified working against your server. The publish-to-mediamtx
path is new and untested end-to-end on my side -- I don't have Docker
available in this session to run it. Verify it (step 5 below) before
pointing Projectivy at it.

## 1. Copy this folder to your server

`Dockerfile`, `app.py`, `docker-compose.yml`, `.env.example` -- keep them
together in one folder, e.g. `immich-rtsp/`.

## 2. Create your `.env`

```
cp .env.example .env
```

Edit it: at minimum set `IMMICH_SERVER_URL` and `IMMICH_API_KEY`. Defaults:
20 photos, 15 seconds each, refreshed every 24h.

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
-- you should see it list album photo counts, "Synced N images", then
"Starting ffmpeg: ..." with no repeating crash/restart after that.

Then test the RTSP feed itself from any machine on your network:
```
ffplay rtsp://<server-ip>:8554/wallpaper
```
or open that same URL in VLC (Media -> Open Network Stream). You should see
your photos slowly cross-cutting. If it doesn't connect: check both
containers are running (`docker ps`), check port 8554 isn't blocked by a
firewall on the server, and check `immich-rtsp`'s logs for an ffmpeg error
(it should mention actually connecting/publishing to mediamtx now, not a
"Connection refused").

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
- Photos are Immich's "preview" size (not full originals), same ~10-15MB
  footprint as the original adb-push script -- just living on your server's
  disk now, not the TV's.
- If the container restarts, it reuses whatever's cached in `/data` (a
  named Docker volume) until the next scheduled refresh, so a quick
  restart doesn't hammer your Immich server.
