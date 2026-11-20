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

VERSION = "0.2.0"
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


def wav_info(path):
    """Return format tag, channels, rate, bits, data offset and length of a RIFF/RF64 WAV."""
    import struct
    with open(path, "rb") as f:
        head = f.read(12)
        if head[:4] not in (b"RIFF", b"RF64") or head[8:12] != b"WAVE":
            return None
        info, pos = {}, 12
        size = os.path.getsize(path)
        while pos + 8 <= size:
            f.seek(pos)
            ck, ln = f.read(4), struct.unpack("<I", f.read(4))[0]
            if ck == b"fmt ":
                d = f.read(min(ln, 40))
                tag, ch, sr = struct.unpack("<HHI", d[:8])
                bits = struct.unpack("<H", d[14:16])[0]
                if tag == 0xFFFE and len(d) >= 26:
                    tag = struct.unpack("<H", d[24:26])[0]
                info.update(tag=tag, ch=ch, sr=sr, bits=bits)
            elif ck == b"data":
                if ln == 0xFFFFFFFF or pos + 8 + ln > size:
                    ln = size - pos - 8
                info.update(off=pos + 8, len=ln)
                break
            pos += 8 + ln + (ln & 1)
        return info if "off" in info and "tag" in info else None


def data_sha(path, info):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        f.seek(info["off"])
        left = info["len"]
        while left > 0:
            b = f.read(min(left, 8 << 20))
            if not b:
                break
            h.update(b)
            left -= len(b)
    return h.hexdigest()


FLAC_TOOL = "afconvert" if shutil.which("afconvert") else ("flac" if shutil.which("flac") else None)


async def run_tool(*cmd):
    p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await p.communicate()
    if p.returncode != 0:
        log("tool failed:", " ".join(cmd[:3]), err.decode(errors="replace")[-300:])
    return p.returncode == 0


async def flac_to_wav(src, dst, bits):
    if FLAC_TOOL == "afconvert":
        return await run_tool("afconvert", "-f", "WAVE", "-d", f"LEI{bits}", src, dst)
    return await run_tool("flac", "-s", "-d", "-f", "-o", dst, src)


async def wav_to_flac(src, dst):
    if FLAC_TOOL == "afconvert":
        return await run_tool("afconvert", "-f", "flac", "-d", "flac", src, dst)
    return await run_tool("flac", "-s", "-f", "-5", "-o", dst, src)


UPLOADS = {}  # upload id -> {"path", "parts"}


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
            "Access-Control-Max-Age": "600", "Access-Control-Allow-Private-Network": "true",
            "Access-Control-Expose-Headers": "X-KJD-SHA, X-KJD-Rate, X-KJD-Bits, X-KJD-Version"}


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


def ws_frame(data, op=0x2):
    n = len(data)
    if n < 126:
        hdr = bytes([0x80 | op, n])
    elif n < 65536:
        hdr = bytes([0x80 | op, 126]) + n.to_bytes(2, "big")
    else:
        hdr = bytes([0x80 | op, 127]) + n.to_bytes(8, "big")
    return hdr + data


async def ws_accept(writer, headers):
    key = headers.get("sec-websocket-key", "")
    accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
    protos = [p.strip() for p in headers.get("sec-websocket-protocol", "").split(",") if p.strip()]
    extra = f"Sec-WebSocket-Protocol: {'binary' if 'binary' in protos else protos[0]}\r\n" if protos else ""
    writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                  f"Sec-WebSocket-Accept: {accept}\r\n{extra}\r\n").encode())
    await writer.drain()


async def ws_read_until_close(reader):
    """Drain client frames (pings, close) until the socket closes."""
    while True:
        h = await reader.readexactly(2)
        ln = h[1] & 0x7F
        if ln == 126:
            ln = int.from_bytes(await reader.readexactly(2), "big")
        elif ln == 127:
            ln = int.from_bytes(await reader.readexactly(8), "big")
        await reader.readexactly((4 if h[1] & 0x80 else 0) + ln)
        if h[0] & 0x0F == 8:
            return


AUDIO_BIN = os.environ.get("KJD_AUDIO_CMD") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "kjd-audio")
AUDIO_PROC = {"p": None}


async def ws_audio(reader, writer, headers, q):
    """Live RX monitoring: 16-bit PCM over a WebSocket. Never written to disk."""
    cmd = AUDIO_BIN.split() if os.environ.get("KJD_AUDIO_CMD") else [AUDIO_BIN]
    if not (os.path.exists(cmd[0]) or shutil.which(cmd[0])):
        return await send(writer, 501, {"error": "Live audio helper isn't installed — run the installer again (needs Apple's Command Line Tools)"}, req_headers=headers)
    low = (q.get("mode") or [""])[0] == "low"
    old = AUDIO_PROC["p"]
    if old and old.returncode is None:
        old.kill()
    proc = await asyncio.create_subprocess_exec(*cmd, "RX 12", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    AUDIO_PROC["p"] = proc
    await ws_accept(writer, headers)
    writer.write(ws_frame(json.dumps({"sr": 24000 if low else 48000, "ch": 1 if low else 2}).encode(), 0x1))
    await writer.drain()
    log("audio stream started" + (" (low data)" if low else ""))

    async def pump():
        import array
        carry = b""
        while True:
            chunk = await proc.stdout.read(9600)
            if not chunk:
                return
            if low:  # 48 kHz stereo -> 24 kHz mono
                chunk = carry + chunk
                usable = len(chunk) - len(chunk) % 8
                carry = chunk[usable:]
                a = array.array("h", chunk[:usable])
                if sys.byteorder != "little":
                    a.byteswap()
                m = array.array("h", ((a[i] + a[i + 1]) // 2 for i in range(0, len(a), 4)))
                if sys.byteorder != "little":
                    m.byteswap()
                chunk = m.tobytes()
            writer.write(ws_frame(chunk))
            await writer.drain()

    async def errs():
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            log("audio:", line.decode(errors="replace").strip())

    tasks = [asyncio.create_task(pump()), asyncio.create_task(ws_read_until_close(reader)), asyncio.create_task(errs())]
    try:
        await asyncio.wait(tasks[:2], return_when=asyncio.FIRST_COMPLETED)
    except Exception:
        pass
    for t in tasks:
        t.cancel()
    if proc.returncode is None:
        proc.kill()
    log("audio stream stopped")


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
        if path.startswith("/api") or path in ("/vnc", "/audio"):
            log(method, path, "from", headers.get("origin", "-"))
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
        if path == "/audio" and headers.get("upgrade", "").lower() == "websocket":
            return await ws_audio(reader, writer, headers, q)
        if path == "/api/ping":
            vnc = False
            try:
                _, w = await asyncio.wait_for(asyncio.open_connection(VNC_HOST, VNC_PORT), 1.5)
                w.close()
                vnc = True
            except Exception:
                pass
            return await send(writer, 200, {"ok": True, "version": VERSION, "rx": bool(RX_APP) or DRY,
                                             "rx_app": os.path.basename(RX_APP or "") or None, "screen_sharing": vnc, "audio": bool(os.path.exists(AUDIO_BIN.split()[0]) or shutil.which(AUDIO_BIN.split()[0])), "flac": bool(FLAC_TOOL),
                                             "host": os.uname().nodename}, req_headers=headers)
        if path == "/api/upload" and method == "POST":  # one part of a chunked upload (resumable, shows progress)
            uid = re.sub(r"[^0-9a-zA-Z]", "", (q.get("id") or [""])[0])[:40]
            part = int((q.get("part") or ["0"])[0])
            body = await read_body(reader, headers)
            if not uid:
                return await send(writer, 400, {"error": "missing id"}, req_headers=headers)
            up = UPLOADS.setdefault(uid, {"path": os.path.join(WORK, ".uploads", uid), "parts": 0})
            os.makedirs(os.path.dirname(up["path"]), exist_ok=True)
            if part == up["parts"] - 1:  # retry of a part we already have
                return await send(writer, 200, {"ok": True, "parts": up["parts"]}, req_headers=headers)
            if part != up["parts"]:
                return await send(writer, 409, {"error": "out of order", "parts": up["parts"]}, req_headers=headers)
            with open(up["path"], "ab" if part else "wb") as f:
                f.write(body)
            up["parts"] += 1
            return await send(writer, 200, {"ok": True, "parts": up["parts"]}, req_headers=headers)
        if path == "/api/jobs" and method == "POST":
            uid = re.sub(r"[^0-9a-zA-Z]", "", (q.get("upload") or [""])[0])[:40]
            if uid:
                up = UPLOADS.pop(uid, None)
                if not up or not os.path.exists(up["path"]):
                    return await send(writer, 400, {"error": "upload not found"}, req_headers=headers)
                with open(up["path"], "rb") as f:
                    body = f.read()
                os.remove(up["path"])
            else:
                body = await read_body(reader, headers)
            want_sha = (q.get("sha") or [""])[0]
            bits = int((q.get("bits") or ["24"])[0] or 24)
            if body[:4] == b"fLaC":  # lossless in transit -> back to WAV for RX
                if not FLAC_TOOL:
                    return await send(writer, 415, {"error": "no FLAC decoder on this Mac", "retry": "wav"}, req_headers=headers)
                tmpd = os.path.join(WORK, ".uploads")
                os.makedirs(tmpd, exist_ok=True)
                fsrc, fdst = os.path.join(tmpd, uuid.uuid4().hex + ".flac"), os.path.join(tmpd, uuid.uuid4().hex + ".wav")
                with open(fsrc, "wb") as f:
                    f.write(body)
                ok = await flac_to_wav(fsrc, fdst, bits)
                os.remove(fsrc)
                info = wav_info(fdst) if ok else None
                if not info or (want_sha and data_sha(fdst, info) != want_sha):
                    if os.path.exists(fdst):
                        os.remove(fdst)
                    log("FLAC upload failed verification — asking for WAV")
                    return await send(writer, 422, {"error": "verify failed", "retry": "wav"}, req_headers=headers)
                with open(fdst, "rb") as f:
                    body = f.read()
                os.remove(fdst)
            if body[:4] not in (b"RIFF", b"RF64"):
                return await send(writer, 400, {"error": "send a WAV file"}, req_headers=headers)
            name = safe((q.get("name") or [unquote(headers.get("x-kjd-name", "clip"))])[0])
            proj = safe((q.get("project") or [unquote(headers.get("x-kjd-project", "KJD Edit"))])[0])
            folder = os.path.join(WORK, proj)
            os.makedirs(folder, exist_ok=True)
            jid = uuid.uuid4().hex[:10]
            p = os.path.join(folder, f"{name} [{jid[:4]}].wav")
            with open(p, "wb") as f:
                f.write(body)
            if want_sha:
                info = wav_info(p)
                if not info or data_sha(p, info) != want_sha:
                    os.remove(p)
                    return await send(writer, 422, {"error": "upload damaged in transit — try again"}, req_headers=headers)
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
                info = wav_info(job["path"])
                hdrs = {"X-KJD-Version": str(job["version"])}
                src, ctype = job["path"], "audio/wav"
                if info:
                    hdrs.update({"X-KJD-SHA": data_sha(job["path"], info), "X-KJD-Rate": str(info["sr"]), "X-KJD-Bits": str(info["bits"])})
                    if (q.get("fmt") or [""])[0] == "flac" and FLAC_TOOL and info["tag"] == 1 and info["bits"] in (16, 24):
                        fl = f'{job["path"]}.v{job["version"]}.flac'
                        if os.path.exists(fl) or await wav_to_flac(job["path"], fl):
                            src, ctype = fl, "audio/flac"
                with open(src, "rb") as f:
                    data = f.read()
                return await send(writer, 200, data, ctype, hdrs, req_headers=headers)
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
