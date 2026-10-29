#!/usr/bin/env python3
"""tv-screenshot: take Apple TV screenshots with a short press of the Siri Remote's Siri button.

    python tv_screenshot.py pair          one-time pairing (enter the PIN shown on the TV)
    python tv_screenshot.py snap [FILE]   take one screenshot now
    python tv_screenshot.py run           the service: button trigger + local HTTP endpoint

Configuration is environment variables only; see README.md.
"""

import asyncio
import json
import logging
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from contextlib import AsyncExitStack
from datetime import datetime
from io import BytesIO
from pathlib import Path

from PIL import Image
from pymobiledevice3.remote import tunnel_service
from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
from pymobiledevice3.remote.userspace_tunnel import UserspaceDialPlane
from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
from pymobiledevice3.services.dvt.instruments.screenshot import Screenshot
from pymobiledevice3.services.os_trace import OsActivityStreamFlag, OsTraceService

env = os.environ.get
OUT_DIR = Path(env("TV_SCREENSHOT_DIR", "~/Pictures/Apple TV Screenshots")).expanduser()
UDID = env("TV_SCREENSHOT_UDID")  # pick one Apple TV when several are paired
BUTTON = env("TV_SCREENSHOT_BUTTON", "Microphone")  # backboardd's name for the Siri button
SHORT_PRESS = float(env("TV_SCREENSHOT_SHORT_PRESS", "0.6"))  # longer holds are real Siri requests
HTTP_HOST = env("TV_SCREENSHOT_HTTP_HOST") or None  # None = all interfaces, IPv4 and IPv6
HTTP_PORT = int(env("TV_SCREENSHOT_HTTP_PORT", "8765"))  # 0 disables the HTTP endpoint
JPEG_QUALITY = int(env("TV_SCREENSHOT_JPEG_QUALITY", "90"))
PLEX_URL, PLEX_TOKEN = env("TV_SCREENSHOT_PLEX_URL"), env("TV_SCREENSHOT_PLEX_TOKEN")
RADARR_URL, RADARR_KEY = env("TV_SCREENSHOT_RADARR_URL"), env("TV_SCREENSHOT_RADARR_API_KEY")
PHOTOS_ALBUM = env("TV_SCREENSHOT_PHOTOS_ALBUM")  # macOS Photos album to import into
RCLONE_DEST = env("TV_SCREENSHOT_RCLONE_DEST")  # e.g. "r2:bucket/screenshots"

TIMEOUT = 20
# backboardd logs every remote button at default level; leaving out DEBUG skips its 60 Hz touchpad chatter.
BUTTON_FLAGS = OsActivityStreamFlag.PAYLOAD | OsActivityStreamFlag.INFO
BUTTON_RE = re.compile(r"^(\w+) page:0x\w+ usage:0x\w+ downEvent:([01])")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
logging.getLogger("pymobiledevice3").setLevel(logging.WARNING)
log = logging.getLogger("tv-screenshot")


# ---------------------------------------------------------------- Apple TV connection

class AppleTV:
    """One rootless RemotePairing tunnel to the Apple TV, shared by screenshots and the button stream."""

    def __init__(self):
        self.stack = self.rsd = self.screenshot = None
        self.ip = None
        self.lock = asyncio.Lock()

    async def connect(self):
        tunnel_service.USE_USERSPACE_TUNNEL = True  # pure-Python TCP/IP stack: no root, no utun
        stack = AsyncExitStack()
        try:
            services = await tunnel_service.get_remote_pairing_tunnel_services(udid=UDID)
            services.sort(key=lambda s: ":" in s.hostname)  # prefer IPv4
            for s in services:
                stack.push_async_callback(s.close)
            if not services:
                raise RuntimeError("no paired Apple TV found (asleep, not paired, or Local Network access blocked)")
            tr = await stack.enter_async_context(services[0].start_tcp_tunnel())
            tr.client.tun.set_peer(tr.address)
            dial = await stack.enter_async_context(UserspaceDialPlane(tr.client.tun, tr.address))
            rsd = RemoteServiceDiscoveryService((tr.address, tr.port), open_connection=dial.dial,
                                                auxiliary_metadata=tr.auxiliary_metadata)
            stack.push_async_callback(rsd.close)
            await rsd.connect()
            self.screenshot = await stack.enter_async_context(
                Screenshot(await stack.enter_async_context(DvtProvider(rsd))))
        except BaseException:
            await stack.aclose()
            raise
        self.stack, self.rsd, self.ip = stack, rsd, services[0].hostname
        log.info("connected to %s %s at %s", rsd.product_type, rsd.product_version, self.ip)

    async def disconnect(self):
        stack, self.stack, self.rsd, self.screenshot = self.stack, None, None, None
        if stack:
            try:
                await asyncio.wait_for(stack.aclose(), 5)
            except BaseException as e:
                log.warning("teardown: %r", e)

    async def ensure(self):
        if self.screenshot is None:
            await asyncio.wait_for(self.connect(), TIMEOUT)

    async def capture(self):
        """PNG bytes of the current screen. Reconnects once; exits (for the supervisor to restart) if that fails."""
        async with self.lock:
            for attempt in (1, 2):
                try:
                    await self.ensure()
                    return await asyncio.wait_for(self.screenshot.get_screenshot(), TIMEOUT)
                except Exception as e:
                    await self.disconnect()
                    if attempt == 2:
                        log.error("capture failed twice (%r); exiting so launchd/systemd restarts us", e)
                        asyncio.get_running_loop().call_later(0.5, os._exit, 1)
                        raise
                    log.warning("capture failed (%r); reconnecting", e)

    @staticmethod
    async def buttons(rsd):
        """Yield (button_name, is_down) for every Siri Remote button, read from backboardd's log."""
        async with OsTraceService(rsd) as trace:
            pids = (await trace.get_pid_list()).get("Payload", {})
        backboardd = next(int(k) for k, v in pids.items() if v.get("ProcessName") == "backboardd")
        async for entry in OsTraceService(rsd).syslog(pid=backboardd, stream_flags=BUTTON_FLAGS):
            if entry.label and entry.label.category == "Button":
                m = BUTTON_RE.match(entry.message)
                if m:
                    yield m.group(1), m.group(2) == "1"


atv = AppleTV()


# ---------------------------------------------------------------- what's playing (optional)

def _get_json(url, headers):
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15) as r:
        return json.load(r)


def _plex(path):
    return _get_json(f"{PLEX_URL.rstrip('/')}{path}",
                     {"Accept": "application/json", "X-Plex-Token": PLEX_TOKEN or ""})["MediaContainer"]


def _radarr(path):
    return _get_json(f"{RADARR_URL.rstrip('/')}/api/v3{path}", {"X-Api-Key": RADARR_KEY or ""})


_tmdb_cache = {}


def _tmdb_id(item):
    """Plex's own TMDB match; else the Radarr movie that owns the file; else a Radarr (TMDB) search,
    accepted only when exactly one result has the same title and year. Never a fuzzy guess."""
    key = item.get("ratingKey")
    if key in _tmdb_cache:
        return _tmdb_cache[key]
    found = next((g["id"][7:] for g in item.get("Guid", []) if g["id"].startswith("tmdb://")), None)
    if found is None and RADARR_URL and item.get("type") == "movie":
        files = [p["file"] for m in item.get("Media", []) for p in m.get("Part", [])]
        for movie in _radarr("/movie"):
            if any(f == movie.get("movieFile", {}).get("path") or f.startswith(movie["path"].rstrip("/") + "/")
                   for f in files):
                found = movie["tmdbId"]
                break
        if found is None and item.get("year"):
            term = urllib.parse.quote(f"{item['title']} {item['year']}")
            exact = [r for r in _radarr(f"/movie/lookup?term={term}")
                     if r["title"].casefold() == item["title"].casefold() and r.get("year") == item["year"]]
            found = exact[0]["tmdbId"] if len(exact) == 1 else None
    _tmdb_cache[key] = found
    return found


def now_playing():
    """What Plex says this Apple TV is playing (Plex and Infuse both report there), or None."""
    if not PLEX_URL:
        return None
    session = next((s for s in _plex("/status/sessions").get("Metadata", [])
                    if s.get("Player", {}).get("address") == atv.ip), None)
    if session is None:
        return None
    item = (_plex(f"/library/metadata/{session['ratingKey']}?includeGuids=1").get("Metadata") or [session])[0]
    title = session["title"]
    if session.get("type") == "episode":
        title = (f"{session.get('grandparentTitle')} "
                 f"S{int(session.get('parentIndex', 0)):02d}E{int(session.get('index', 0)):02d} {title}")
    return {"title": title, "year": session.get("year"), "tmdb": _tmdb_id(item),
            "episode": session.get("type") == "episode", "offset": int(session.get("viewOffset", 0)) // 1000}


async def describe(taken):
    """(file name without extension, caption, upload path without extension) for a shot taken at `taken`.
    Uploads are filed by TMDB id: <id>/01h23m45s, tv/<episode id>/01h23m45s, else unmatched/<timestamp>."""
    try:
        np = await asyncio.to_thread(now_playing)
    except Exception as e:
        log.warning("now-playing lookup failed: %r", e)
        np = None
    if not np:
        return (f"No media playing {taken:%Y-%m-%d %H.%M.%S}", f"No media playing · {taken:%Y-%m-%d %H:%M:%S}",
                f"unmatched/{taken:%Y-%m-%d-%H%M%S}")
    h, rem = divmod(np["offset"], 3600)
    m, s = divmod(rem, 60)
    year = f" ({np['year']})" if np["year"] else ""
    tmdb = f" [tmdb-{np['tmdb']}]" if np["tmdb"] else ""
    position = f"{h:02d}h{m:02d}m{s:02d}s"
    name = re.sub(r'[/:\\"*?<>|]+', "-", f"{np['title']}{year}{tmdb} {position}")
    caption = " · ".join(filter(None, [f"{np['title']}{year}", f"{h}:{m:02d}:{s:02d}",
                                       np["tmdb"] and f"TMDB {np['tmdb']}", f"{taken:%Y-%m-%d %H:%M}"]))
    if not np["tmdb"]:
        return name, caption, f"unmatched/{taken:%Y-%m-%d-%H%M%S}"
    return name, caption, f"{'tv/' if np['episode'] else ''}{np['tmdb']}/{position}"


def to_jpeg(png):
    out = BytesIO()
    Image.open(BytesIO(png)).convert("RGB").save(out, "JPEG", quality=JPEG_QUALITY, subsampling=0)
    return out.getvalue()


# ---------------------------------------------------------------- destinations (optional)

PHOTOS_SCRIPT = """
on run argv
    set albumName to item 2 of argv
    tell application "Photos"
        if not (exists album albumName) then make new album named albumName
        repeat with mediaItem in (import {POSIX file (item 1 of argv)} into album albumName skip check duplicates true)
            set description of mediaItem to item 3 of argv
        end repeat
    end tell
end run
"""


async def _run(*cmd, stdin=None, timeout=300):
    proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.PIPE if stdin else None,
                                                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await asyncio.wait_for(proc.communicate(stdin), timeout)
    if proc.returncode:
        raise RuntimeError(err.decode(errors="replace").strip()[-500:])


async def to_photos(path, caption):
    await _run("osascript", "-", str(path), PHOTOS_ALBUM, caption, stdin=PHOTOS_SCRIPT.encode())


async def to_rclone(path, remote_path):
    await _run("rclone", "copyto", str(path), f"{RCLONE_DEST.rstrip('/')}/{remote_path}", "--immutable")


# Each enabled destination leaves a sidecar file next to the shot until it succeeds, holding what it needs
# (Photos: the caption; rclone: the upload path); failures retry on the next shot.
DESTINATIONS = [d for d in [(".photos", PHOTOS_ALBUM, to_photos), (".rclone", RCLONE_DEST, to_rclone)] if d[1]]


async def deliver_pending():
    for suffix, label, deliver in DESTINATIONS:
        for sidecar in sorted(OUT_DIR.glob(f"*{suffix}")):
            shot = sidecar.with_suffix(".jpg")
            try:
                if shot.exists():
                    await deliver(shot, sidecar.read_text())
                    log.info("%s -> %s", shot.name, label)
                sidecar.unlink()
            except Exception as e:
                log.warning("%s -> %s failed, will retry on the next shot: %s", shot.name, label, e)
                break


_delivering = asyncio.Lock()  # one delivery pass at a time, or a sidecar could be delivered twice


async def take_shot(keep=None):
    """Capture now, then save and deliver. `keep` resolves once the button is released;
    False (a Siri hold, not a shutter press) discards the capture."""
    taken = datetime.now()
    png = await atv.capture()
    if keep is not None and not await asyncio.wait_for(keep, TIMEOUT):
        log.info("long press (Siri), capture discarded")
        return None
    name, caption, remote = await describe(taken)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path, n, dup = OUT_DIR / f"{name}.jpg", 2, ""
    while path.exists():  # never overwrite
        path, dup, n = OUT_DIR / f"{name} {n}.jpg", f"-{n}", n + 1
    path.write_bytes(to_jpeg(png))
    sidecars = {".photos": caption, ".rclone": f"{remote}{dup}.jpg"}
    for suffix, _, _ in DESTINATIONS:
        path.with_suffix(suffix).write_text(sidecars[suffix])
    log.info("saved %s", path.name)
    async with _delivering:
        await deliver_pending()
    return path


# ---------------------------------------------------------------- triggers

async def watch_remote():
    """A short press of BUTTON takes a shot. Reconnects whenever the stream drops (e.g. the Apple TV slept).

    The capture starts on button-down, not release, so the frame is the one on screen when the button
    went down; releasing after SHORT_PRESS (a Siri request) throws it away. Presses during a capture
    are ignored, since a queued capture would land seconds late."""
    while True:
        rsd = None
        try:
            async with atv.lock:
                await atv.ensure()
                rsd = atv.rsd
            pressed = keep = None
            async for button, down in atv.buttons(rsd):
                if button != BUTTON:
                    continue
                if down:
                    if atv.lock.locked():
                        log.info("press ignored: a capture is still in progress")
                        continue
                    pressed, keep = time.monotonic(), asyncio.get_running_loop().create_future()
                    asyncio.create_task(_shot_from_remote(keep))
                elif keep and not keep.done():
                    keep.set_result(time.monotonic() - pressed < SHORT_PRESS)
        except Exception as e:
            log.warning("button stream: %r", e)
        async with atv.lock:
            if atv.rsd is rsd:  # don't tear down a connection a capture already rebuilt
                await atv.disconnect()
        await asyncio.sleep(5)


async def _shot_from_remote(keep):
    try:
        await take_shot(keep)
    except Exception as e:
        log.error("shot failed: %r", e)


async def _ping(rsd):
    async with OsTraceService(rsd) as trace:
        await trace.get_pid_list()


async def keepalive():
    """The tunnel can die without the button stream noticing; a press would then wait out TIMEOUT before
    reconnecting. A cheap round trip every minute finds that first."""
    while True:
        await asyncio.sleep(60)
        rsd = atv.rsd
        if rsd is None or atv.lock.locked():
            continue
        try:
            await asyncio.wait_for(_ping(rsd), 10)
        except Exception as e:
            log.warning("keepalive failed (%r); reconnecting", e)
            async with atv.lock:
                if atv.rsd is rsd:
                    await atv.disconnect()  # also ends the button stream, which reconnects


async def handle_http(reader, writer):
    """GET /snap (JPEG) or /snap.png (raw 16-bit) returns the screen without saving it; GET /health."""
    try:
        line = (await asyncio.wait_for(reader.readline(), 10)).decode(errors="replace").split()
        while (await asyncio.wait_for(reader.readline(), 10)) not in (b"\r\n", b"\n", b""):
            pass
        path = line[1].split("?")[0] if len(line) > 1 else ""
        status, ctype, body, extra = 404, "text/plain", b"not found\n", ""
        if path == "/health":
            status, ctype = 200, "application/json"
            body = json.dumps({"connected": atv.screenshot is not None, "apple_tv": atv.ip}).encode()
        elif path in ("/snap", "/snap.png"):
            try:
                png = await atv.capture()
                name, *_ = await describe(datetime.now())
                ext = "png" if path == "/snap.png" else "jpg"
                status, ctype = 200, f"image/{'png' if ext == 'png' else 'jpeg'}"
                body = png if ext == "png" else to_jpeg(png)
                extra = f"Content-Disposition: inline; filename*=UTF-8''{urllib.parse.quote(name)}.{ext}\r\n"
            except Exception as e:
                status, ctype, body = 503, "application/json", json.dumps({"error": repr(e)}).encode()
        reason = {200: "OK", 404: "Not Found", 503: "Service Unavailable"}[status]
        writer.write(f"HTTP/1.1 {status} {reason}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
                     f"Cache-Control: no-store\r\nConnection: close\r\n{extra}\r\n".encode() + body)
        await writer.drain()
    except Exception as e:
        log.warning("http: %r", e)
    finally:
        writer.close()


async def run():
    tasks = [asyncio.create_task(watch_remote()), asyncio.create_task(keepalive())]
    if HTTP_PORT:
        server = await asyncio.start_server(handle_http, HTTP_HOST, HTTP_PORT)
        log.info("HTTP on port %d", HTTP_PORT)
        tasks.append(asyncio.create_task(server.serve_forever()))
    await asyncio.gather(*tasks)


# ---------------------------------------------------------------- pairing

async def pair():
    from pymobiledevice3.bonjour import browse_remotepairing_manual_pairing
    from pymobiledevice3.exceptions import RemotePairingCompletedError
    from pymobiledevice3.remote.tunnel_service import RemotePairingManualPairingService

    print("Looking for an Apple TV on Settings > Remotes and Devices > Remote App and Devices ...")
    answers = await browse_remotepairing_manual_pairing()
    if not answers:
        sys.exit("None found. Open that screen on the Apple TV (same network as this machine) and try again.")
    answer = answers[0]
    ip = next((a.full_ip for a in answer.addresses if "." in a.full_ip), answer.addresses[0].full_ip)
    print(f"Pairing with {answer.properties['name']} at {ip}. Type the PIN shown on the TV (it expires in ~1 min).")
    async with RemotePairingManualPairingService(answer.properties["identifier"], ip, answer.port) as service:
        try:
            await service.connect(autopair=True)
        except RemotePairingCompletedError:
            pass
    print("Paired. Try: python tv_screenshot.py snap")


async def snap_once(dest):
    png = await atv.capture()
    name, *_ = await describe(datetime.now())
    path = Path(dest or f"{name}.png")
    path.write_bytes(png if path.suffix.lower() == ".png" else to_jpeg(png))
    print(path)
    await atv.disconnect()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "pair":
        asyncio.run(pair())
    elif cmd == "snap":
        asyncio.run(snap_once(sys.argv[2] if len(sys.argv) > 2 else None))
    elif cmd == "run":
        asyncio.run(run())
    else:
        sys.exit(__doc__)
