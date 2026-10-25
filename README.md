# tv-screenshot

Take a screenshot of your Apple TV by tapping the Siri button on the remote.

A tiny always-on service (one Python file) on a Mac on the same network watches the Apple TV for a **short press of the Siri button**. Each press saves a full-resolution screenshot, named after what's playing if you use Plex:

```
The Bone Collector (1999) [tmdb-9481] 00h01m22s.jpg
No media playing 2026-10-17 09.39.10.jpg
```

Shots are saved to a folder, and can optionally go into a macOS Photos album (and from there iCloud) and/or any [rclone](https://rclone.org) remote (S3, R2, Google Drive, ...). A small local HTTP endpoint (`/snap`) also returns the current screen on demand.

No jailbreak, no app on the Apple TV, no root on the Mac.

## How it works

- **Connection.** tv-screenshot pairs with the Apple TV the same way Xcode's wireless debugging does (RemotePairing), using [pymobiledevice3](https://github.com/doronz88/pymobiledevice3). The tunnel runs on pymobiledevice3's userspace TCP/IP stack, so it needs no root and no `utun` interface.
- **Trigger.** tvOS logs every Siri Remote button press in `backboardd`'s system log (`BackBoard:Button Microphone ... downEvent:1`). tv-screenshot streams only that process's log and fires on a press of the Siri button shorter than 0.6 s. The capture starts the moment the button goes down (about 50 ms after the press) and is discarded if the button turns out to be held, so the shot is the frame you pressed on, not the one when you let go. A short Siri press does nothing visible during video playback, so it makes a silent shutter. Holding the button still invokes Siri as normal.
- **Screenshot.** The same developer screenshot service Xcode uses: a 16-bit PNG at the TV's output resolution, converted to JPEG (4:4:4, quality 90).
- **Naming (optional).** Plex `/status/sessions` says what this Apple TV is playing; Infuse and the Plex app both report there. The TMDB id comes from Plex's match, else the Radarr movie that owns the file, else a Radarr/TMDB search accepted only if exactly one result has the same title and year.

## Requirements

| | |
|---|---|
| Apple TV | tvOS 17 or later (developed on Apple TV 4K 3rd gen, tvOS 26.6) |
| Host | An always-on Mac on the same LAN (developed on macOS 27). Linux should work for the core features but is untested; the Photos destination is macOS-only. |
| Python | **3.13+ built against OpenSSL** (python.org or Homebrew `python@3.13`). Not Apple's `/usr/bin/python3`: it uses LibreSSL, which can't negotiate the tunnel's TLS-PSK cipher. |
| Python packages | `pymobiledevice3`, pinned in `requirements.txt` (tv-screenshot uses its userspace-tunnel internals, which can change between releases). Pillow comes with it. |
| Optional | Plex Media Server (naming), Radarr (TMDB ids for unmatched Plex items), rclone (cloud upload), macOS Photos (album import). |

## Install

```sh
git clone https://github.com/<you>/tv-screenshot && cd tv-screenshot
/opt/homebrew/bin/python3.13 -m venv .venv      # any OpenSSL-based Python 3.13+
.venv/bin/pip install -r requirements.txt
```

## Pair (once)

1. On the Apple TV open **Settings > Remotes and Devices > Remote App and Devices** and leave it on that screen.
2. Run `.venv/bin/python tv_screenshot.py pair` and type the PIN the TV shows. The PIN expires after about a minute.
3. Test: `.venv/bin/python tv_screenshot.py snap` writes a PNG of the current screen.

The pairing record lives in `~/.pymobiledevice3/`. Re-pair after resetting the Apple TV or removing the Mac from that Settings screen.

## Run as a service (macOS)

```sh
cp launchd/tv-screenshot.plist ~/Library/LaunchAgents/
# edit the paths and the environment variables you want, then:
chmod 600 ~/Library/LaunchAgents/tv-screenshot.plist   # if it holds tokens
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/tv-screenshot.plist
tail -f ~/Library/Logs/tv-screenshot.log
```

It must be a LaunchAgent (logged-in user session), not a LaunchDaemon. On first start macOS asks whether **Python may find devices on your local network**: allow it (System Settings > Privacy & Security > Local Network). With a Photos album set, the first import also asks to let Python control Photos.

Then tap the Siri button on the remote.

## Configuration

Environment variables (all optional):

| Variable | Default | |
|---|---|---|
| `TV_SCREENSHOT_DIR` | `~/Pictures/Apple TV Screenshots` | Where shots are saved |
| `TV_SCREENSHOT_BUTTON` | `Microphone` | backboardd's button name to trigger on. Others: `Menu`, `Select`, `Left`, `Right`, `ToggleMute` |
| `TV_SCREENSHOT_SHORT_PRESS` | `0.6` | Max seconds held to count as a shutter press |
| `TV_SCREENSHOT_UDID` | first paired | Choose an Apple TV when several are paired |
| `TV_SCREENSHOT_HTTP_PORT` | `8765` | `0` disables the HTTP endpoint |
| `TV_SCREENSHOT_HTTP_HOST` | all interfaces | e.g. `127.0.0.1` to keep it local to the host |
| `TV_SCREENSHOT_JPEG_QUALITY` | `90` | |
| `TV_SCREENSHOT_PLEX_URL` / `TV_SCREENSHOT_PLEX_TOKEN` | off | Name shots after what Plex says is playing. On a Mac running Plex, the token is `defaults read com.plexapp.plexmediaserver PlexOnlineToken` |
| `TV_SCREENSHOT_RADARR_URL` / `TV_SCREENSHOT_RADARR_API_KEY` | off | TMDB ids for movies Plex didn't match |
| `TV_SCREENSHOT_PHOTOS_ALBUM` | off | Import each shot into this Photos album (created if missing), with a caption |
| `TV_SCREENSHOT_RCLONE_DEST` | off | Upload each shot with `rclone copyto ... --immutable`, e.g. `r2:my-bucket/screenshots`. Uses your rclone config (`RCLONE_CONFIG` to point at a dedicated one) |

When a Photos import or upload fails, a sidecar file (`.photos` / `.rclone`) stays next to the shot and the delivery is retried after the next shot. Files are never overwritten; a repeated name gets ` 2`, ` 3`, ...

## HTTP endpoint and remote access

| | |
|---|---|
| `GET /snap` | JPEG of the current screen (not saved), named via `Content-Disposition` |
| `GET /snap.png` | Raw 16-bit PNG (~20 MB at 4K) |
| `GET /health` | `{"connected": true, "apple_tv": "192.168.0.142"}` |

It is plain HTTP with **no authentication**, meant for your LAN: `http://<mac-name>.local:8765/snap` works from an iPhone Shortcut ("Get Contents of URL" then "Save to Photo Album").

The host itself must stay on the Apple TV's local network: pairing discovery and the device tunnel both run over the LAN. To reach the endpoint from outside, don't port-forward it. Use a **Cloudflare Tunnel with Access** instead: route a hostname to `http://localhost:8765`, protect it with an Access service-token policy, and send the `CF-Access-Client-Id` / `CF-Access-Client-Secret` headers from the Shortcut.

## Limitations

- **Undocumented interfaces.** The trigger depends on the wording of a tvOS system log line, and the connection on Apple's developer pairing. A tvOS update could break either.
- **Only the Siri button works well as a shutter.** Play/Pause is logged identically to a touchpad click (`Select`), and every other button has a visible effect.
- **DRM-protected video** (Netflix, Apple TV+, ...) captures as black, as with any screenshot tool.
- **Naming needs Plex.** Without a Plex session for this Apple TV, a shot is `No media playing <time>`, even if another app is playing. Plex's position is exact while paused but can lag about 10 s during playback. A movie paused long enough for the screensaver still has a Plex session, so a screensaver shot then gets the movie's name.
- **The "Screenshot Taken" banner** tvOS shows after each capture appears in a second shot taken within a few seconds.
- **About 2 s per shot**: a 4K 16-bit PNG crosses a pure-Python network stack.
- **macOS Local Network permission** is tied to the Python binary. After upgrading Python, re-allow it and recreate the venv.
- **Photos can't import into shared albums** (Photos' AppleScript only reaches regular albums). To get shots onto another iCloud account's devices, sign the Mac into that account or use rclone.
- **One Apple TV per running instance.**

## Troubleshooting

- `no paired Apple TV found`: the Apple TV is asleep (it reconnects when woken), it isn't paired, or Local Network access is blocked. Check the log for `bonjour discovery is blocked`.
- Pairing fails after typing the PIN: it probably expired; run `pair` again and type it right away.
- Works from a terminal but not as a LaunchAgent: Local Network permission. A process started over SSH borrows the SSH session's permission and loses it once the session ends.

## License

GPL-3.0-or-later, the same as pymobiledevice3, which tv-screenshot is built on.
