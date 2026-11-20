#!/usr/bin/env python3
"""KJD RX Bridge — lets KJD Edit on the iPad use iZotope RX 12 Advanced on this Mac.

Standard library only (no installs). Runs a small local server that:
  • receives a clip from KJD Edit, opens it in RX 12, and hands back the processed file each time you press ⌘S in RX
  • relays macOS Screen Sharing (VNC, port 5900) to the RX window inside KJD Edit, so you can see and click RX from the iPad

Reach it from the iPad through Tailscale (`tailscale serve` gives this Mac a private https:// address).
Every request needs the pairing token shown by the launcher.
"""
import asyncio, base64, glob, hashlib, json, os, re, secrets, shutil, subprocess, sys, time, uuid
from urllib.parse import urlsplit, parse_qs, unquote

VERSION = "0.1.0"
APP_NAME = "KJD RX Bridge"
SUPPORT = os.path.expanduser(f"~/Library/Application Support/{APP_NAME}")
WORK = os.path.expanduser("~/Music/KJD Edit RX")
CONFIG = os.path.join(SUPPORT, "config.json")
PORT = int(os.environ.get("KJD_BRIDGE_PORT", "8770"))
VNC_HOST, VNC_PORT = "127.0.0.1", int(os.environ.get("KJD_VNC_PORT", "5900"))
MAX_UPLOAD = 4 * 1024 ** 3
DRY = sys.platform != "darwin" or os.environ.get("KJD_BRIDGE_DRY") == "1"


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def load_config():
    os.makedirs(SUPPORT, exist_ok=True)
    cfg = {}
    if os.path.exists(CONFIG):
        try:
            cfg = json.load(open(CONFIG))
        except Exception:
            cfg = {}
    if not cfg.get("token"):
        cfg["token"] = secrets.token_urlsafe(18)
        json.dump(cfg, open(CONFIG, "w"), indent=1)
    return cfg


def find_rx_app(configured=""):
    if configured and os.path.exists(configured):
        return configured
    pats = ["/Applications/iZotope RX 12*/*.app", "/Applications/*RX 12*Audio Editor*.app",
            "/Applications/iZotope*/*RX 12*.app", os.path.expanduser("~/Applications/*RX 12*.app")]
    for p in pats:
        hits = sorted(glob.glob(p))
        hits = [h for h in hits if "Audio Editor" in h] or hits
        if hits:
            return hits[0]
    return None


CFG = load_config()
TOKEN = CFG["token"]
RX_APP = find_rx_app(CFG.get("rx_path", ""))
JOBS = {}  # id -> {path, name, project, mtime, size, version, created}


def safe(s):
    return re.sub(r'[\\/:*?"<>|]+', "_", str(s)).strip()[:120] or "clip"


def open_in_rx(path):
    if DRY or not RX_APP:
        log("(dry) open in RX:", path)
        return
    subprocess.Popen(["open", "-a", RX_APP, path])


def save_in_rx():
    if DRY or not RX_APP:
        log("(dry) ⌘S in RX")
        return True
    proc = os.path.splitext(os.path.basename(RX_APP))[0].replace('"', '')
    script = (f'tell application "System Events" to tell process "{proc}"\n set frontmost to true\n delay 0.3\n'
              ' keystroke "s" using command down\nend tell')
    return subprocess.run(["osascript", "-e", script], capture_output=True).returncode == 0


def file_state(job):
    try:
        st = os.stat(job["path"])
    except FileNotFoundError:
        return "missing"
    # a save counts once the file changed and has stopped growing for ~0.8 s
    if (st.st_mtime_ns, st.st_size) != (job["mtime"], job["size"]):
        if job.get("_pending") != (st.st_mtime_ns, st.st_size):
            job["_pending"] = (st.st_mtime_ns, st.st_size)
            job["_since"] = time.time()
        elif time.time() - job["_since"] > 0.8:
            job["mtime"], job["size"] = st.st_mtime_ns, st.st_size
            job["version"] += 1
            job.pop("_pending", None)
    return "saved" if job["version"] > 0 else "open"


# ---------------------------------------------------------------- tiny HTTP/1.1 + WebSocket server
REASONS = {200: "OK", 204: "No Content", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found",
           405: "Method Not Allowed", 413: "Payload Too Large", 500: "Internal Server Error", 502: "Bad Gateway"}


def cors_headers(req_headers):
    origin = req_headers.get("origin", "*")
    return {"Access-Control-Allow-Origin": origin, "Vary": "Origin",
            "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, X-KJD-Token, X-KJD-Name, X-KJD-Project",
            "Access-Control-Max-Age": "600", "Access-Control-Allow-Private-Network": "true"}


async def send(writer, status, body=b"", ctype="application/json", headers=None, req_headers=None):
    if isinstance(body, (dict, list)):
        body = json.dumps(body).encode()
    elif isinstance(body, str):
        body = body.encode()
    h = {"Content-Type": ctype, "Content-Length": str(len(body)), "Connection": "close", "Cache-Control": "no-store"}
    h.update(cors_headers(req_headers or {}))
    h.update(headers or {})
    head = f"HTTP/1.1 {status} {REASONS.get(status, 'OK')}\r\n" + "".join(f"{k}: {v}\r\n" for k, v in h.items()) + "\r\n"
    writer.write(head.encode() + body)
    await writer.drain()


async def read_body(reader, headers):
    if headers.get("transfer-encoding", "").lower() == "chunked":
        out = bytearray()
        while True:
            line = await reader.readline()
            n = int(line.strip().split(b";")[0] or b"0", 16)
            if n == 0:
                await reader.readline()
                return bytes(out)
            out += await reader.readexactly(n)
            await reader.readexactly(2)
            if len(out) > MAX_UPLOAD:
                raise ValueError("too large")
    n = int(headers.get("content-length", "0") or 0)
    if n > MAX_UPLOAD:
        raise ValueError("too large")
    return await reader.readexactly(n) if n else b""


async def ws_vnc(reader, writer, headers):
    key = headers.get("sec-websocket-key", "")
    accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
    protos = [p.strip() for p in headers.get("sec-websocket-protocol", "").split(",") if p.strip()]
    try:
        vr, vw = await asyncio.open_connection(VNC_HOST, VNC_PORT)
    except OSError:
        await send(writer, 502, {"error": "Screen Sharing is not on (nothing on port 5900)"}, req_headers=headers)
        return
    extra = f"Sec-WebSocket-Protocol: {'binary' if 'binary' in protos else protos[0]}\r\n" if protos else ""
    writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                  f"Sec-WebSocket-Accept: {accept}\r\n{extra}\r\n").encode())
    await writer.drain()
    log("screen view connected")

    async def ws_to_vnc():
        buf = bytearray()
        while True:
            h = await reader.readexactly(2)
            fin, op = h[0] & 0x80, h[0] & 0x0F
            masked, ln = h[1] & 0x80, h[1] & 0x7F
            if ln == 126:
                ln = int.from_bytes(await reader.readexactly(2), "big")
            elif ln == 127:
                ln = int.from_bytes(await reader.readexactly(8), "big")
            mask = await reader.readexactly(4) if masked else b"\0\0\0\0"
            data = bytearray(await reader.readexactly(ln))
            if masked:
                for i in range(ln):
                    data[i] ^= mask[i & 3]
            if op == 8:
                return
            if op == 9:  # ping -> pong
                writer.write(bytes([0x8A, len(data)]) + data if len(data) < 126 else b"")
                await writer.drain()
                continue
            if op in (0, 1, 2):
                buf += data
                if fin:
                    vw.write(bytes(buf))
                    await vw.drain()
                    buf.clear()

    async def vnc_to_ws():
        while True:
            data = await vr.read(262144)
            if not data:
                return
            n = len(data)
            if n < 126:
                hdr = bytes([0x82, n])
            elif n < 65536:
                hdr = bytes([0x82, 126]) + n.to_bytes(2, "big")
            else:
                hdr = bytes([0x82, 127]) + n.to_bytes(8, "big")
            writer.write(hdr + data)
            await writer.drain()

    tasks = [asyncio.create_task(ws_to_vnc()), asyncio.create_task(vnc_to_ws())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except Exception:
        pass
    for t in tasks:
        t.cancel()
    vw.close()
    log("screen view closed")


async def handle(reader, writer):
    headers = {}
    try:
        line = await asyncio.wait_for(reader.readline(), 30)
        if not line:
            return
        method, target, _ = line.decode("latin-1").split(" ", 2)
        while True:
            h = await reader.readline()
            if h in (b"\r\n", b"\n", b""):
                break
            k, _, v = h.decode("latin-1").partition(":")
            headers[k.strip().lower()] = v.strip()
        u = urlsplit(target)
        path, q = unquote(u.path), parse_qs(u.query)
        if method == "OPTIONS":
            return await send(writer, 204, b"", req_headers=headers)
        tok = headers.get("x-kjd-token") or (q.get("token") or [""])[0]
        if path == "/":
            return await send(writer, 200, f"{APP_NAME} {VERSION} is running. Open KJD Edit on your iPad › File › RX Bridge Setup.",
                              "text/plain; charset=utf-8", req_headers=headers)
        if not secrets.compare_digest(tok, TOKEN):
            return await send(writer, 401, {"error": "bad token"}, req_headers=headers)
        if path == "/vnc" and headers.get("upgrade", "").lower() == "websocket":
            return await ws_vnc(reader, writer, headers)
        if path == "/api/ping":
            vnc = False
            try:
                _, w = await asyncio.wait_for(asyncio.open_connection(VNC_HOST, VNC_PORT), 1.5)
                w.close()
                vnc = True
            except Exception:
                pass
            return await send(writer, 200, {"ok": True, "version": VERSION, "rx": bool(RX_APP) or DRY,
                                             "rx_app": os.path.basename(RX_APP or "") or None, "screen_sharing": vnc,
                                             "host": os.uname().nodename}, req_headers=headers)
        if path == "/api/jobs" and method == "POST":
            body = await read_body(reader, headers)
            if body[:4] not in (b"RIFF", b"RF64"):
                return await send(writer, 400, {"error": "send a WAV file"}, req_headers=headers)
            name = safe(unquote(headers.get("x-kjd-name", "clip")))
            proj = safe(unquote(headers.get("x-kjd-project", "KJD Edit")))
            folder = os.path.join(WORK, proj)
            os.makedirs(folder, exist_ok=True)
            jid = uuid.uuid4().hex[:10]
            p = os.path.join(folder, f"{name} [{jid[:4]}].wav")
            with open(p, "wb") as f:
                f.write(body)
            st = os.stat(p)
            JOBS[jid] = {"path": p, "name": name, "project": proj, "mtime": st.st_mtime_ns, "size": st.st_size,
                         "version": 0, "created": time.time()}
            open_in_rx(p)
            log(f"job {jid}: {name} ({len(body)/1048576:.1f} MB) opened in RX")
            return await send(writer, 200, {"id": jid, "file": os.path.basename(p)}, req_headers=headers)
        m = re.fullmatch(r"/api/jobs/([0-9a-f]{10})(/file|/reopen|/save)?", path)
        if m:
            job = JOBS.get(m.group(1))
            if not job:
                return await send(writer, 404, {"error": "unknown job (bridge restarted?)"}, req_headers=headers)
            sub = m.group(2)
            if sub is None and method == "GET":
                return await send(writer, 200, {"state": file_state(job), "version": job["version"],
                                                "file": os.path.basename(job["path"])}, req_headers=headers)
            if sub == "/file":
                with open(job["path"], "rb") as f:
                    data = f.read()
                return await send(writer, 200, data, "audio/wav", {"X-KJD-Version": str(job["version"])}, req_headers=headers)
            if sub == "/reopen":
                open_in_rx(job["path"])
                return await send(writer, 200, {"ok": True}, req_headers=headers)
            if sub == "/save":  # press ⌘S in RX for you (needs Accessibility permission for KJD RX Bridge)
                ok = save_in_rx()
                return await send(writer, 200 if ok else 500, {"ok": ok} if ok else {"error": "Allow KJD RX Bridge under System Settings › Privacy & Security › Accessibility"}, req_headers=headers)
            if sub is None and method == "DELETE":
                JOBS.pop(m.group(1), None)
                return await send(writer, 200, {"ok": True}, req_headers=headers)
        return await send(writer, 404, {"error": "not found"}, req_headers=headers)
    except (asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError):
        pass
    except ValueError:
        try:
            await send(writer, 413, {"error": "file too large"}, req_headers=headers)
        except Exception:
            pass
    except Exception as e:
        log("error:", repr(e))
        try:
            await send(writer, 500, {"error": str(e)}, req_headers=headers)
        except Exception:
            pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def main():
    os.makedirs(WORK, exist_ok=True)
    srv = await asyncio.start_server(handle, "127.0.0.1", PORT, limit=1 << 20)
    log(f"{APP_NAME} {VERSION} on 127.0.0.1:{PORT} · RX: {RX_APP or 'not found'} · work folder: {WORK}")
    async with srv:
        await srv.serve_forever()


if __name__ == "__main__":
    if "--token" in sys.argv:
        print(TOKEN)
        sys.exit(0)
    asyncio.run(main())
