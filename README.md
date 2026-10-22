# Sony KD-65X7500F (Android TV 9) debloat project

TV connected over network ADB at `192.168.1.XXX:5555`. All commands below
assume `tools/tv.py` as the ADB wrapper (`TV_HOST`/`TV_PORT` env vars, or
`.tv_host` file, pick the target; `ADB_BIN` picks the adb binary).

## What this TV already had going in

This was not a stock TV -- it already had ~28 sideloaded apps (Plex,
Jellyfin, Crunchyroll, Netflix, Kodi, VLC, NordVPN, Steam Link, SmartTube,
various AU streaming apps, etc.) and **Projectivy Launcher**
(`com.spocky.projengmenu`) was already installed and set as the active Home
launcher, replacing the stock one. None of those were touched.

## What got disabled

46 packages disabled via `pm disable-user --user 0`, never uninstalled. Full
list: `backup/disabled_by_us.log`. Batches (see `batches/*.txt` for exact
package lists, and `shots/` for before/after screenshots at each step):

1. **Telemetry/diagnostics/demo mode** -- Sony bug-report tools, log-level
   settings, Samba TV (ACR/ad-tracking SDK), demo/retail modes, customer
   support/remote-access tools, service-engineer mode (disabled, never
   launched -- launching factory-mode apps can brick CEC/Input on some
   boards, so we only ever inspected these with `dumpsys package`, never
   `am start`).
2. **Hotel/B2B (commercial) mode** -- not relevant to a home install.
3. **Content/recommendation/ad services** -- Sony Select, Discovery,
   BIVL info, Kenh Chon Loc, pre-install-app-filtering.
4. **Miracast/DLNA casting** -- disabled since only Chromecast is used
   (confirmed with you). `com.google.android.apps.mediashell` (Chromecast)
   was left alone.
5. **HbbTV/broadcast-interactive apps and the IR blaster** -- disabled since
   neither is used (confirmed with you). Voice search
   (`com.google.android.katniss`) was left alone.
6. **Stock Google TV Launcher** (`com.google.android.tvlauncher`) -- this
   was still installed and had the *highest* Home-intent priority on the
   device, so Android briefly snapped Home back to its ad-laden UI
   ("Mega Movie Week", "Buy for $5.99") when a package-state change reset
   the Home preference mid-debloat. Disabled so only Projectivy can ever
   claim Home again. Verified with `dumpsys activity activities | grep
   mResumedActivity` after a reboot.
7. **Misc low-value preloaded apps** -- manual/help viewer, second-display
   setting.

**Note:** `com.sony.dtv.imanual` and `com.sony.dtv.seconddispsetting`
(batch 7) were found re-enabled by the firmware after a reboot. This board
re-enables a small set of packages at every boot; re-disabling them each
time isn't worth fighting. Everything else in the log stayed disabled
across the reboot (verified: `backup/post-reboot-disabled.txt`).

### What was deliberately left alone

- Everything Android-core: permission controller, package installer, ext
  services/shared, module metadata, `com.android.providers.*` (except the
  usual calendar/contacts/userdict exclusions, which weren't touched
  either), systemui, shell, phone, settings, tv.settings, `android`.
- TV input/tuner (`com.sony.dtv.tvinput.*`), remote control/BT pairing,
  on-screen keyboard (`com.sony.dtv.ime.chww`, `imeproxy`), Play Store,
  Play Services.
- HDMI-CEC / Bravia Sync (`com.sony.dtv.braviasync*`) -- needed for
  controlling other HDMI devices from this remote.
- `com.sony.dtv.systemupdate` (OTA updates), DIAL protocol service
  (`com.sony.dtv.dialservice` -- used by some apps' "cast" features),
  MediaTek SoC agents (tuner-adjacent, didn't want to risk it).
- All 28 of your sideloaded streaming/utility apps.

### `tools/disable.sh` safety behaviour

Refuses to run (with no changes made) if the batch file contains anything
matching the protected-package patterns baked into the script -- core
Android packages or this device's functional packages (TV input, remote,
keyboard, Play Store/Services). One run hit a `SecurityException` from the
firmware on a single package (`com.sony.dtv.kenhchonloc`) that still ended
up disabled despite the error -- the script trusts `pm list packages -d`
over exit codes for exactly this reason; check that file if you're ever
unsure what actually happened.

## Launcher

**Not replaced.** You already use Projectivy Launcher and were happy with
it, so the custom Apple-TV-style launcher from the original plan was never
built. See "Immich wallpaper" below for the one launcher-adjacent thing we
did configure.

## Immich photos as the launcher background

Projectivy's wallpaper source was "Reddit" (hence the mountain/canyon
photos). To use your own Immich library instead, we went through a few
designs before landing on the current one:

- **Current setup, verified working on the TV: `docker/immich-rtsp/`.** Two
  small containers on your own server (deployed as a Portainer stack):
  `mediamtx` (a real, independently maintained RTSP server) and
  `immich-rtsp` (a small Python+ffmpeg publisher that pulls photos from
  Immich and publishes a looping slideshow to it). Projectivy's **Media
  file** wallpaper source (free, not Premium-gated) points its Uri at
  `rtsp://<server-ip>:8554/wallpaper` with the Video toggle on -- no file
  pushing to the TV, no local storage growth, genuinely live. Confirmed
  rotating through real photos from both albums directly on the TV screen.
  Getting there took a few rounds of debugging (Portainer's BuildKit was
  broken unrelated to this project; ffmpeg's own built-in RTSP server mode
  turned out unreliable, replaced with mediamtx; mediamtx's default 10s
  timeout was killing the publisher) -- see `docker/immich-rtsp/README.md`
  and that folder's git history for the detail. `LANDSCAPE_ONLY=true`
  (default) filters out portrait photos, which otherwise show letterboxed
  with black bars down the sides.
- You already have Projectivy Premium, so the **Folder** source (point it
  at a local image directory on the TV) is also available if you'd rather
  not run the RTSP service. `tools/sync_immich_wallpapers.py` (runs from a
  PC) or `docs/IMMICH_SYNC_SERVER_SETUP.md` (runs from your server via cron
  + adb) both push a capped, randomly-sampled set of photos to
  `/sdcard/Pictures/ImmichSync` on the TV for that source to read.
- We also tried running a sync *on* the TV itself via Termux -- a plain
  `pkg install python cronie` pulled in a 470MB clang/llvm/make toolchain,
  way too heavy for a daily photo refresh, so Termux was installed and then
  removed again.
- A plain native Live Wallpaper app (self-hosted, no server) was considered
  too, but ruled out: confirmed via `dumpsys window` that Projectivy's main
  window doesn't carry `FLAG_SHOW_WALLPAPER`, so it never composites the
  Android system wallpaper behind itself at all -- a Live Wallpaper
  wouldn't show through Projectivy no matter how it was built.

Credentials live in `secrets/immich.env` (gitignored, never commit it) and
in `docker/immich-rtsp/.env` on the server (also gitignored).

## Restoring everything

```
python tools/restore.py
```

Re-enables every package in `backup/disabled_by_us.log`. Reboot afterward
to confirm everything comes back. This does not touch Termux (already
removed) or anything about the launcher (Projectivy was never disabled).

## Closing down ADB when you're done for the session

Per the original plan: when you're done working with the TV over ADB,
switch off **USB debugging** and **Network/ADB debugging** in Developer
Options, revoke USB debugging authorizations, and reboot. Then verify:

```
python tools/adb-closed.py 192.168.1.XXX
```

This device is wired over its built-in Ethernet port (confirmed via
`dumpsys usb | grep host_connected` = false and `dumpsys connectivity`
showing `Transports: ETHERNET` on `eth0`, not a USB adapter), so there's no
USB-adapter power-cycle concern here when you flip debugging off.

Note: if you set up the server-side Immich sync (above), it depends on
network ADB staying on. Turning off Network/ADB debugging will break the
sync's cron job silently until you turn it back on and re-run
`adb connect` from the server.

## Everything else (screenshots, batches, raw backups)

- `shots/` -- a screenshot after every change, in order, including the
  Home-launcher flakiness investigation and the Projectivy settings
  exploration that found the Wallpaper/Folder option.
- `backup/<timestamp>/` -- full package list, disabled-package baseline,
  `getprop`, all settings namespaces, display size/density, at the start of
  the session, before anything was touched.
- `batches/` -- the exact package lists passed to `tools/disable.sh` for
  each step above.
