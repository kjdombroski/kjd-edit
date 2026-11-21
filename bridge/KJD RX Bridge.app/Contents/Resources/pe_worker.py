#!/usr/bin/env python3
"""Piano Excerpts engine for KJD Edit on the iPad.

Started by KJD RX Bridge with the Piano Excerpts app's own Python (its venv) and the app's code on
PYTHONPATH, so the iPad uses exactly the same analysis, selection, naming, metadata and export as the
Mac app. One request per line on stdin (JSON), replies on stdout:
  {"id": n, "progress": 0..1, "msg": "..."}   while working
  {"id": n, "ok": true, "result": {...}}       or {"id": n, "ok": false, "error": "..."}
Sessions are the Mac app's .pxs files: the iPad's edits are appended to the session's history, so the
Mac app opens them with every earlier version still there.
"""
from __future__ import annotations

import datetime as dt
import glob
import hashlib
import json
import os
import pickle
import shutil
import subprocess
import sys
import tempfile
import time
import traceback

PROTO = sys.stdout
sys.stdout = sys.stderr          # library prints must never corrupt the protocol

import numpy as np                # noqa: E402
import soundfile as sf            # noqa: E402

from piano_excerpts import __version__ as PE_VERSION                      # noqa: E402
from piano_excerpts import session as sess                                # noqa: E402
from piano_excerpts.analysis import MARKINGS, analyze, describe_span      # noqa: E402
from piano_excerpts.selection import Excerpt, derive_alternates, find_excerpts, parse_order  # noqa: E402

WORKER_VERSION = "0.1.0"
HOME = os.path.expanduser("~")
SUPPORT = os.path.join(HOME, "Library", "Application Support", "KJD RX Bridge")
CACHE = os.path.join(SUPPORT, "pe_cache")
TMP = os.path.join(tempfile.gettempdir(), "kjd-pe-previews")
EX_FIELDS = ("start", "end", "fade_in", "fade_out", "curve_in", "curve_out", "enabled", "rx_path", "rx_t0",
             "number", "rank", "score", "nominal")
PROJECT_SUBDIRS = ("Bounced Excerpts", "RX Files", "Session File Backups")
AUDIO_EXT = (".wav", ".wave", ".aif", ".aiff", ".flac", ".bwf")
ST = {"an": None, "sources": [], "session": None, "rx_full": None, "side": "before"}


def send(obj):
    PROTO.write(json.dumps(obj) + "\n")
    PROTO.flush()


def jnum(v):
    if isinstance(v, np.generic):
        return v.item()
    return v


def len_label(seconds):
    m, s = divmod(int(round(seconds)), 60)
    return f"{m}:{s:02d}"


def parse_length(v) -> float:
    v = str(v).strip()
    if ":" in v:
        sec = 0.0
        for p in v.split(":"):
            sec = sec * 60 + float(p)
        return sec
    return float(v.rstrip("s"))


def ex_to_dict(e: Excerpt) -> dict:
    d = {f: jnum(getattr(e, f)) for f in EX_FIELDS}
    d["description"] = e.description
    return d


def ex_from_dict(d: dict) -> Excerpt:
    e = Excerpt(float(d["start"]), float(d["end"]), float(d.get("score") or 0.0))
    for f in EX_FIELDS:
        if f in d and not (f in ("number", "rank", "score", "rx_t0", "enabled") and d[f] is None):
            setattr(e, f, d[f])
    if e.rx_path and not os.path.exists(e.rx_path):
        e.rx_path = None
    return e


def need_an():
    if ST["an"] is None:
        raise RuntimeError("Open a recording first.")
    return ST["an"]


def allowed(path: str) -> bool:
    p = os.path.realpath(path)
    return p.startswith(HOME + os.sep) or p == HOME or p.startswith("/Volumes/")


# ---------------------------------------------------------------- browsing
def cmd_browse(a, prog):
    path = a.get("path") or ""
    if not path:
        roots = [{"name": n, "path": os.path.join(HOME, n), "dir": True}
                 for n in ("Desktop", "Music", "Documents", "Downloads", "Movies")
                 if os.path.isdir(os.path.join(HOME, n))]
        roots += [{"name": n, "path": os.path.join("/Volumes", n), "dir": True}
                  for n in sorted(os.listdir("/Volumes")) if os.path.isdir(os.path.join("/Volumes", n))
                  and not os.path.islink(os.path.join("/Volumes", n))] if os.path.isdir("/Volumes") else []
        recent = [r for r in _recent() if os.path.exists(r)]
        return {"path": "", "parent": None, "entries": roots,
                "recent": [{"name": os.path.basename(r), "path": r, "dir": False} for r in recent]}
    if not allowed(path):
        raise RuntimeError("That folder isn't available from the iPad.")
    try:
        names = sorted(os.listdir(path), key=str.lower)
    except PermissionError:
        raise RuntimeError("macOS blocked this folder. On the Mac: System Settings › Privacy & Security › "
                           "Files & Folders (or Full Disk Access) › allow Python, then try again.")
    out = []
    for n in names:
        if n.startswith("."):
            continue
        p = os.path.join(path, n)
        if os.path.isdir(p) and not n.endswith(".app"):
            out.append({"name": n, "path": p, "dir": True})
        elif n.lower().endswith(AUDIO_EXT + (".pxs",)):
            try:
                st = os.stat(p)
                out.append({"name": n, "path": p, "dir": False, "size": st.st_size, "mtime": st.st_mtime})
            except OSError:
                pass
    parent = os.path.dirname(path.rstrip("/"))
    if path.rstrip("/") in [os.path.join(HOME, x) for x in ("Desktop", "Music", "Documents", "Downloads", "Movies")] \
            or os.path.dirname(path.rstrip("/")) == "/Volumes":
        parent = ""
    return {"path": path, "parent": parent, "entries": out}


def _recent():
    try:
        prefs = json.load(open(os.path.join(HOME, "Library", "Application Support", "Piano Excerpts", "prefs.json")))
        r = prefs.get("recent_sessions") or []
    except Exception:
        r = []
    try:
        r2 = json.load(open(os.path.join(SUPPORT, "pe_recent.json")))
    except Exception:
        r2 = []
    seen, out = set(), []
    for p in r2 + r:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out[:10]


def _remember(path):
    r = [p for p in _recent() if p != path]
    os.makedirs(SUPPORT, exist_ok=True)
    try:
        json.dump([path] + r[:9], open(os.path.join(SUPPORT, "pe_recent.json"), "w"))
    except Exception:
        pass


# ---------------------------------------------------------------- analysis (cached)
def load_analysis(src, prog):
    st = os.stat(src)
    key = hashlib.sha1(f"{os.path.realpath(src)}|{st.st_size}|{st.st_mtime_ns}|{PE_VERSION}".encode()).hexdigest()
    cp = os.path.join(CACHE, key + ".pkl")
    if os.path.exists(cp):
        try:
            with open(cp, "rb") as f:
                an = pickle.load(f)
            prog(1.0, "Analysis loaded from cache")
            return an
        except Exception:
            pass
    an = analyze(src, progress=lambda f, m="": prog(f * 0.98, m or "Analyzing"))
    try:
        os.makedirs(CACHE, exist_ok=True)
        old = sorted(glob.glob(os.path.join(CACHE, "*.pkl")), key=os.path.getmtime)
        for o in old[:-11]:
            os.remove(o)
        with open(cp + ".tmp", "wb") as f:
            pickle.dump(an, f)
        os.replace(cp + ".tmp", cp)
    except Exception as err:
        print("cache write failed:", err)
    return an


def overview(an, points=6000):
    n = an.n_hops
    k = max(1, int(np.ceil(n / points)))
    m = (n // k) * k
    pk = an.peak_db[:m].reshape(-1, k).max(axis=1) if m else an.peak_db
    lv = an.level[:m].reshape(-1, k).mean(axis=1) if m else an.level
    act = an.active[:m].reshape(-1, k).mean(axis=1) if m else an.active.astype(float)
    pos = np.asarray(an.dynamics.position(lv))
    return {"hop": an.hop_s * k, "peak": [round(float(x), 1) for x in pk],
            "level": [round(float(x), 1) for x in lv], "pos": [round(float(x), 3) for x in pos],
            "active": [round(float(x), 2) for x in act],
            "thresholds": [[mk, round(float(v), 2)] for mk, v in an.dynamics.thresholds()]}


def an_info(an):
    return {"path": an.path, "name": os.path.basename(an.path), "sr": an.sr, "channels": an.channels,
            "subtype": an.subtype, "duration": an.duration, "integrated_lufs": round(an.integrated_lufs, 1),
            "lra": round(an.lra_lu, 1), "peak_db": round(an.sample_peak_db, 1),
            "floor": round(an.dynamics.floor_lufs, 1), "ceiling": round(an.dynamics.ceiling_lufs, 1),
            "overview": overview(an)}


def cmd_open_audio(a, prog):
    from piano_excerpts.stereo import resolve_sources
    paths = a["paths"]
    for p in paths:
        if not allowed(p) or not os.path.exists(p):
            raise RuntimeError(f"Can't find {os.path.basename(p)} on the Mac.")
    prog(0.01, "Checking channels")
    src = resolve_sources(paths, log=lambda m: prog(None, m), progress=lambda f: prog(f * 0.2, "Making stereo file"))
    an = load_analysis(src, prog)
    ST.update(an=an, sources=list(paths), session=None, rx_full=None, side="before")
    return {"analysis": an_info(an), "sources": paths}


# ---------------------------------------------------------------- sessions (.pxs, shared with the Mac app)
def sets_from_session(data):
    hist = data.get("history") or []
    meta = {int(k): v for k, v in (data.get("set_meta") or {}).items()}
    cur = int(data.get("cur_set", 0) or 0)
    ids = sorted({int(h.get("set", 0)) for h in hist}) or [cur]
    sets = []
    for sid in ids:
        entries = [h for h in hist if int(h.get("set", 0)) == sid]
        exs = data.get("excerpts") if sid == cur else (entries[-1]["snap"]["exs"] if entries else [])
        label = next((h.get("set_label") for h in reversed(entries) if h.get("set_label")), "") if entries else ""
        m = meta.get(sid) or {}
        if not m.get("length") and exs:
            nom = exs[0].get("nominal") or (exs[0]["end"] - exs[0]["start"])
            m = {"length": len_label(nom), "root": sid}
        sets.append({"id": sid, "label": label or f"{len(exs)} excerpts", "meta": m, "exs": exs or []})
    return sets, cur


def describe_all(an, exs):
    for d in exs:
        d["description"] = describe_span(an, float(d["start"]), float(d["end"]))
    return exs


def cmd_open_session(a, prog):
    from piano_excerpts.stereo import resolve_sources
    path = a["path"]
    if not allowed(path):
        raise RuntimeError("That session isn't available from the iPad.")
    data = sess.load(path)
    srcs = data.get("sources") or []
    src = data.get("analysis_source")
    if not (src and os.path.exists(src)):
        missing = [p for p in srcs if not os.path.exists(p)]
        if missing or not srcs:
            raise RuntimeError("This session's audio was moved or renamed: " +
                               ", ".join(os.path.basename(m) for m in missing) +
                               ". Open the session once on the Mac to relink it.")
        src = resolve_sources(srcs, log=lambda m: prog(None, m))
    an = load_analysis(src, prog)
    rx_full = data.get("rx_full") if data.get("rx_full") and os.path.exists(data.get("rx_full")) else None
    side = data.get("rx_side", "before") if rx_full else "before"
    an.audio_path = rx_full if side == "after" else None
    ST.update(an=an, sources=srcs, session=path, rx_full=rx_full, side=side)
    sets, cur = sets_from_session(data)
    for s in sets:
        describe_all(an, s["exs"])
    ins = data.get("inserts") or []
    n_ins = sum(1 for s in ins if s and (s.get("entry") or s.get("label")))
    _remember(path)
    return {"analysis": an_info(an), "session": path, "name": os.path.splitext(os.path.basename(path))[0],
            "settings": data.get("settings") or {}, "sets": sets, "cur_set": cur, "rx_full": rx_full,
            "rx_side": side, "inserts": n_ins, "saved": data.get("saved"), "sel": data.get("sel"),
            "history_len": len(data.get("history") or [])}


def project_dir():
    return os.path.dirname(ST["session"]) if ST["session"] else None


def cmd_save_session(a, prog):
    an = need_an()
    path = a.get("path") or ST["session"]
    if not path:                          # new: DAW-style project folder next to the recording
        name = (a.get("name") or os.path.splitext(os.path.basename(an.path))[0]).strip()
        for tok in (".L", "_L", "-L", " L"):
            if name.endswith(tok):
                name = name[: -len(tok)]
        name = "".join(c if c not in '/\\:*?"<>|' else "_" for c in name) or "Session"
        folder = a.get("folder") or os.path.dirname(an.path)
        if not allowed(folder):
            raise RuntimeError("Can't save there.")
        proj = os.path.join(folder, name)
        path = os.path.join(proj, name + sess.EXT)
        if os.path.exists(path) and not a.get("overwrite"):
            raise RuntimeError(f"EXISTS:{path}")
        for sub in PROJECT_SUBDIRS:
            os.makedirs(os.path.join(proj, sub), exist_ok=True)
        data = {}
    else:
        data = sess.load(path)
        bk = os.path.join(os.path.dirname(path), "Session File Backups")
        try:
            os.makedirs(bk, exist_ok=True)
            stem = os.path.splitext(os.path.basename(path))[0]
            shutil.copy2(path, os.path.join(bk, f"{stem} {dt.datetime.now():%Y-%m-%d %H%M%S}.pxs"))
            olds = sorted(glob.glob(os.path.join(bk, f"{stem} *.pxs")))
            for o in olds[:-20]:
                os.remove(o)
        except Exception as err:
            print("backup failed:", err)
    hist = data.get("history") or []
    slots = hist[-1]["snap"].get("slots") if hist else [None] * 5
    meta = {str(k): v for k, v in (data.get("set_meta") or {}).items()}
    labels = {}
    for s in a["sets"]:
        sid = int(s["id"])
        meta[str(sid)] = s.get("meta") or meta.get(str(sid)) or {}
        labels[sid] = s.get("label", "")
        for ch in s.get("changes") or []:   # one history point per edit made on the iPad
            exs = [{f: jnum(d.get(f)) for f in EX_FIELDS} for d in ch["exs"]]
            hist.append({"label": ch.get("label", "Edit") + "  (iPad)", "time": ch.get("time") or time.strftime("%H:%M:%S"),
                         "snap": {"path": an.path, "exs": exs, "slots": slots}, "set": sid,
                         "set_label": labels[sid]})
    cur = int(a["cur_set"])
    cur_exs = next((s["exs"] for s in a["sets"] if int(s["id"]) == cur), [])
    if not any(int(h.get("set", 0)) == cur for h in hist):
        hist.append({"label": "Saved  (iPad)", "time": time.strftime("%H:%M:%S"),
                     "snap": {"path": an.path, "exs": [{f: jnum(d.get(f)) for f in EX_FIELDS} for d in cur_exs],
                              "slots": slots}, "set": cur, "set_label": labels.get(cur, "")})
    settings = dict(data.get("settings") or {})
    settings.update({k: str(v) for k, v in (a.get("settings") or {}).items()})
    data.update({
        "sources": data.get("sources") or ST["sources"] or [an.path],
        "analysis_source": an.path,
        "settings": settings,
        "excerpts": [{f: jnum(d.get(f)) for f in EX_FIELDS} for d in cur_exs],
        "sel": a.get("sel", data.get("sel")),
        "history": hist, "hptr": len(hist) - 1, "cur_set": cur, "set_meta": meta,
        "rx_full": ST["rx_full"], "rx_side": ST["side"] if ST["rx_full"] else "before",
        "edited_on": "iPad",
    })
    data.setdefault("inserts", [None] * 5)
    data.setdefault("ab", {"A": None, "B": None})
    data.pop("is_autosave", None)
    sess.save(path, data)
    ST["session"] = path
    _remember(path)
    return {"session": path, "name": os.path.splitext(os.path.basename(path))[0], "history_len": len(hist),
            "saved": data.get("saved")}


# ---------------------------------------------------------------- finding excerpts
def cmd_find(a, prog):
    an = need_an()
    L = parse_length(a.get("length", "1:00"))
    order = a.get("order") or None
    if order:
        parse_order(order)
    prog(0.3, "Scoring windows")
    exs = find_excerpts(an, L, int(a.get("count", 5)), a.get("intensity", "any"),
                        tolerance=float(a.get("tolerance", 10)) / 100, order=order)
    fi, fo = float(a.get("fade_in", 0.05)), float(a.get("fade_out", 1.5))
    for e in exs:
        e.fade_in = min(fi, e.end - e.start)
        e.fade_out = min(fo, e.end - e.start - e.fade_in)
        e.curve_in = e.curve_out = "s-curve"
    return {"exs": [ex_to_dict(e) for e in exs], "length": len_label(L)}


def cmd_alternates(a, prog):
    an = need_an()
    L = parse_length(a["length"])
    parents = [ex_from_dict(d) for d in a["parents"]]
    alts = derive_alternates(an, parents, L, a.get("intensity", "any"), a.get("order") or None)
    return {"exs": [ex_to_dict(e) for e in alts], "length": len_label(L)}


def cmd_describe(a, prog):
    an = need_an()
    return {"items": [describe_span(an, float(s), float(e)) for s, e in a["spans"]]}


# ---------------------------------------------------------------- listening (monitoring copies only)
def cmd_audio(a, prog):
    from piano_excerpts.export import read_range
    an = need_an()
    t0 = max(0.0, float(a["start"]))
    t1 = min(an.duration, float(a["end"]))
    if t1 - t0 > 900:
        raise RuntimeError("Preview is limited to 15 minutes at a time.")
    ex = ex_from_dict(a["ex"]) if a.get("ex") else None
    x = read_range(an, ex, t0, t1, dtype="float32")
    sr = an.sr
    if sr not in (44100, 48000):
        from scipy.signal import resample_poly
        from math import gcd
        g = gcd(48000, sr)
        x = resample_poly(x, 48000 // g, sr // g, axis=0).astype("float32")
        sr = 48000
    if a.get("mono") and x.shape[1] > 1:
        x = x.mean(axis=1, keepdims=True)
    os.makedirs(TMP, exist_ok=True)
    for old in glob.glob(os.path.join(TMP, "*")):
        if time.time() - os.path.getmtime(old) > 600:
            try:
                os.remove(old)
            except OSError:
                pass
    base = os.path.join(TMP, hashlib.sha1(os.urandom(8)).hexdigest()[:12])
    wav = base + ".wav"
    sf.write(wav, np.clip(x, -1, 1), sr, subtype="PCM_16")
    fmt = a.get("fmt", "aac")
    if fmt == "aac" and shutil.which("afconvert"):
        out = base + ".m4a"
        r = subprocess.run(["afconvert", "-f", "m4af", "-d", "aac", "-b", str(int(a.get("kbps", 256)) * 1000), wav, out],
                           capture_output=True)
        if r.returncode == 0:
            os.remove(wav)
            return {"file": out, "type": "audio/mp4", "sr": sr, "start": t0, "end": t1}
    if fmt in ("aac", "flac") and shutil.which("afconvert"):
        out = base + ".flac"
        r = subprocess.run(["afconvert", "-f", "flac", "-d", "flac", wav, out], capture_output=True)
        if r.returncode == 0:
            os.remove(wav)
            return {"file": out, "type": "audio/flac", "sr": sr, "start": t0, "end": t1}
    return {"file": wav, "type": "audio/wav", "sr": sr, "start": t0, "end": t1}


# ---------------------------------------------------------------- export (same engine, names, metadata)
def cmd_export(a, prog):
    from piano_excerpts.export import DEFAULT_TEMPLATE, build_name, export_excerpts
    an = need_an()
    if ST["session"]:
        out_dir = os.path.join(project_dir(), "Bounced Excerpts")
    else:
        stem = os.path.splitext(os.path.basename(an.path))[0]
        out_dir = os.path.join(os.path.dirname(an.path), stem + "_excerpts")
    stem = os.path.splitext(os.path.basename(an.path))[0]
    batches = []
    for b in a["batches"]:
        exs = [ex_from_dict(d) for d in b]
        for e in exs:
            e.description = describe_span(an, e.start, e.end)
        batches.append(exs)
    names = [build_name(DEFAULT_TEMPLATE, stem, e) + ".wav" for b in batches for e in b if e.enabled]
    clash = [n for n in names if os.path.exists(os.path.join(out_dir, n))]
    mode = a.get("mode", "check")
    if mode == "check":
        return {"clash": len(clash), "count": len(names), "out_dir": out_dir,
                "side": ST["side"] if ST["rx_full"] else "before"}
    suffix = ""
    if clash and mode == "keep":
        k = 2
        while any(os.path.exists(os.path.join(out_dir, n[:-4] + f" ({k}).wav")) for n in names):
            k += 1
        suffix = f" ({k})"
    norm = str(a.get("normalize") or "").strip()
    fi, fo = float(a.get("fade_in", 0.05)), float(a.get("fade_out", 1.5))
    rows_all = []
    total = max(1, len(names))
    done = [0]

    def lg(m):
        if isinstance(m, str) and m.strip().startswith(("✓", "  ✓", "Wrote", "  Wrote")):
            done[0] += 1
        prog(min(done[0] / total, 0.99), str(m).strip()[:160])

    for exs in batches:
        rows = export_excerpts(an, exs, out_dir, DEFAULT_TEMPLATE, fi, fo, float(norm) if norm else None,
                               log=lg, slots=None, name_suffix=suffix)
        rows_all += rows
    return {"out_dir": out_dir, "files": [r.get("file") for r in rows_all], "count": len(rows_all),
            "side": ST["side"] if ST["rx_full"] else "before"}


# ---------------------------------------------------------------- RX 12: whole recording, BEFORE / AFTER
def cmd_rx_prepare(a, prog):
    an = need_an()
    stem = os.path.splitext(os.path.basename(an.path))[0]
    folder = os.path.join(project_dir(), "RX Files") if ST["session"] else os.path.dirname(an.path)
    os.makedirs(folder, exist_ok=True)
    dest = os.path.join(folder, f"{stem} (RX).wav")
    reuse = os.path.exists(dest) and ST["rx_full"] == dest and not a.get("fresh")
    if not reuse:
        prog(0.05, f"Copying the whole recording ({os.path.getsize(an.path) / 1e9:.2f} GB)")
        shutil.copy2(an.path, dest)
    return {"path": dest, "reused": reuse}


def cmd_rx_loaded(a, prog):
    an = need_an()
    p = a["path"]
    try:
        info = sf.info(p)
    except Exception as err:
        raise RuntimeError(f"RX file not readable yet ({err})")
    if info.samplerate != an.sr:
        raise RuntimeError(f"RX saved at {info.samplerate} Hz – keep {an.sr} Hz so excerpts stay in sync.")
    if abs(info.frames - an.frames) > an.sr * 0.01:
        raise RuntimeError("The RX version's length changed – not used (excerpts would drift). "
                           "Avoid trimming, Time & Pitch or resampling.")
    ST["rx_full"] = p
    ST["side"] = "after"
    an.audio_path = p
    return {"rx_full": p, "side": "after"}


def cmd_rx_side(a, prog):
    an = need_an()
    side = a.get("side", "before")
    if side == "after" and not (ST["rx_full"] and os.path.exists(ST["rx_full"])):
        raise RuntimeError("Process the whole recording in RX first.")
    ST["side"] = side
    an.audio_path = ST["rx_full"] if side == "after" else None
    return {"side": side, "rx_full": ST["rx_full"]}


def cmd_ping(a, prog):
    return {"worker": WORKER_VERSION, "piano_excerpts": PE_VERSION, "markings": MARKINGS,
            "open": ST["an"].path if ST["an"] is not None else None, "session": ST["session"]}


CMDS = {"ping": cmd_ping, "browse": cmd_browse, "open_audio": cmd_open_audio, "open_session": cmd_open_session,
        "save_session": cmd_save_session, "find": cmd_find, "alternates": cmd_alternates, "describe": cmd_describe,
        "audio": cmd_audio, "export": cmd_export, "rx_prepare": cmd_rx_prepare, "rx_loaded": cmd_rx_loaded,
        "rx_side": cmd_rx_side}


def main():
    send({"id": 0, "ok": True, "result": {"ready": True, "piano_excerpts": PE_VERSION, "worker": WORKER_VERSION}})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        rid = req.get("id")
        last = [0.0]

        def prog(f, m=None, rid=rid):
            now = time.time()
            if now - last[0] < 0.25 and f not in (1.0,):
                return
            last[0] = now
            send({"id": rid, "progress": None if f is None else round(float(f), 3), "msg": m})

        try:
            fn = CMDS[req.get("cmd")]
            send({"id": rid, "ok": True, "result": fn(req.get("args") or {}, prog)})
        except Exception as err:
            traceback.print_exc()
            send({"id": rid, "ok": False, "error": str(err) or err.__class__.__name__})


if __name__ == "__main__":
    main()
