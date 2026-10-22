# immich-rtsp

Serves a looping RTSP slideshow of photos pulled from Immich albums, for use
as Projectivy Launcher's background (Media file source, `rtsp://` Uri,
Video toggle on) -- no file pushing to the TV, no cron+adb pipeline, just
one stream URL that's always live.

**Tested/untested split, honestly:** the Immich-fetching code is the same
logic already verified working against your server (album lookup, asset
search, preview download). The ffmpeg/RTSP serving half is new and
untested -- I don't have Docker or ffmpeg available in this session to run
it end-to-end. Verify it (step 4 below) before pointing Projectivy at it.

## 1. Copy this folder to your server

`Dockerfile`, `app.py`, `docker-compose.yml`, `.env.example` -- keep them
together in one folder, e.g. `immich-rtsp/`.

## 2. Create your `.env`

```
cp .env.example .env
```

Edit it: at minimum set `IMMICH_SERVER_URL` and `IMMICH_API_KEY`. Defaults:
20 photos, 15 seconds each, refreshed every 24h, served on port 8554.

## 3. Build the image once via terminal

The compose file references a pre-built local image (`immich-rtsp:latest`)
rather than building inline -- Portainer's own build pipeline (BuildKit)
turned out to be broken in this setup (`listing workers for Build` /
`http2: frame too large` errors), so building happens here instead, once:

```
git clone https://github.com/ellisben133/immichrtsp.git
cd immichrtsp/docker/immich-rtsp
DOCKER_BUILDKIT=0 docker build -t immich-rtsp:latest .
```

(`DOCKER_BUILDKIT=0` uses Docker's classic builder, sidestepping the same
buildx/BuildKit path that failed in Portainer.) Re-run this build command
any time `app.py` or the `Dockerfile` changes (e.g. after a `git pull`).

## 4. Deploy as a Portainer stack

Portainer -> **Stacks** -> **Add stack** -> **Repository**, URL
`https://github.com/ellisben133/immichrtsp.git`, ref `refs/heads/main`,
compose path `docker/immich-rtsp/docker-compose.yml`. Paste your `.env`
contents into the stack's environment variables. **Deploy the stack** --
since the compose file has no `build:` directive, Portainer just starts
the image you already built, no builder involved.

If you update this repo later, use the stack's **Pull and redeploy**
button -- just remember to re-run the terminal build step first if
`app.py`/`Dockerfile` changed, otherwise it'll redeploy the old image.

## 5. Verify the stream actually works before touching Projectivy

Check the container logs first (`docker logs immich-rtsp` or via Portainer)
-- you should see it list album photo counts and "Synced N images", then
"Starting ffmpeg: ...".

Then test the RTSP feed itself from any machine on your network:
```
ffplay rtsp://<server-ip>:8554/wallpaper
```
or open that same URL in VLC (Media -> Open Network Stream). You should see
your photos slowly cross-cutting. If it doesn't connect: check the
container is still running (`docker ps`), check port 8554 isn't blocked by
a firewall, and check the logs for an ffmpeg error.

## 6. Point Projectivy at it

On the TV, in Projectivy: Settings -> Appearance -> Wallpaper -> Launcher
wallpaper -> **Media file** -> Configure -> set **Uri** to
`rtsp://<server-ip>:8554/wallpaper` and toggle **Video** on.

## Notes

- Only one client can connect to ffmpeg's built-in RTSP listen mode at a
  time by design (fine here -- only the TV connects). If you ever want
  multiple viewers, put MediaMTX in front of it instead of connecting
  directly to ffmpeg.
- Images refresh every `REFRESH_INTERVAL_HOURS` (default 24h); ffmpeg
  restarts at that point, so the TV will show a brief black frame / reconnect
  blip once a day. Not noticeable for a background.
- Photos are Immich's "preview" size (not full originals), same ~10-15MB
  footprint as the original adb-push script -- just living on your server's
  disk now, not the TV's.
- If the container restarts, it reuses whatever's cached in `/data` (a
  named Docker volume) until the next scheduled refresh, so a quick
  restart doesn't hammer your Immich server.
