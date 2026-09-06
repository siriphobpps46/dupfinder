#!/usr/bin/env python3
"""
dupfinder - หาไฟล์ที่เนื้อไฟล์ซ้ำกันเป๊ะ แล้วรีวิว/ลบผ่านหน้าเว็บในเครื่อง

วิธีใช้:
    python3 dupfinder.py                    # เปิด UI ที่ http://127.0.0.1:8777
    python3 dupfinder.py --port 9000        # เปลี่ยนพอร์ต
    python3 dupfinder.py --root ~/Pictures  # ตั้งโฟลเดอร์เริ่มต้น
    python3 dupfinder.py --no-open          # ไม่ต้องเปิดเบราว์เซอร์ให้

ใช้ Python stdlib ล้วน ไม่ต้องลงอะไรเพิ่ม ปุ่มลบไม่ได้ลบจริง แต่ย้ายไปถังพักไฟล์
บนไดรฟ์เดียวกัน และกดคืนกลับได้
"""

import argparse
import hashlib
import html
import json
import mimetypes
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

IS_MAC = sys.platform == "darwin"
IS_WIN = os.name == "nt"

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(HERE, ".state")
# ดัชนีการสแกน = ทิ้งได้ สร้างใหม่ได้เสมอ
INDEX_DB = os.path.join(STATE_DIR, "index.db")
# บัญชีถังพักไฟล์ = ของมีค่า ห้ามลบ เพราะมันคือแผนที่ว่าไฟล์ไหนถูกย้ายมาจากไหน
LEDGER_DB = os.path.join(STATE_DIR, "ledger.db")
THUMB_DIR = os.path.join(STATE_DIR, "thumbs")
QUARANTINE_NAME = "_DUPFINDER_QUARANTINE"
MANIFEST_NAME = "manifest.json"

PARTIAL_BYTES = 65536
CHUNK = 1024 * 1024

SKIP_DIRS = {
    QUARANTINE_NAME,
    # Windows
    "$RECYCLE.BIN", "$Recycle.Bin", "System Volume Information",
    # macOS
    ".Spotlight-V100", ".Trashes", ".fseventsd", ".TemporaryItems",
    ".DocumentRevisions-V100", ".DS_Store",
    # Linux / NAS
    "lost+found", "@eaDir", ".Trash-1000",
    ".git",
}
SKIP_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini", ".localized", "Icon\r"}

THUMBABLE = {
    ".jpg", ".jpeg", ".png", ".gif", ".heic", ".heif", ".tiff", ".tif", ".bmp",
    ".webp", ".dng", ".cr2", ".nef", ".arw", ".psd", ".mov", ".mp4", ".m4v",
    ".avi", ".mkv", ".pdf", ".key", ".pages", ".numbers", ".ai",
}


# --------------------------------------------------------------------------- db

def connect():
    """เปิด index.db เป็นตัวหลัก แล้ว attach ledger.db เข้ามาเป็น `led`

    แยกเป็นสองไฟล์เพื่อให้ล้างดัชนีทิ้งได้โดยไม่แตะบัญชีถังพักไฟล์
    """
    os.makedirs(STATE_DIR, exist_ok=True)
    legacy = os.path.join(STATE_DIR, "dupfinder.db")
    if os.path.exists(legacy) and not os.path.exists(INDEX_DB):
        for suf in ("", "-wal", "-shm"):
            if os.path.exists(legacy + suf):
                os.rename(legacy + suf, INDEX_DB + suf)
    con = sqlite3.connect(INDEX_DB, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("ATTACH DATABASE ? AS led", (LEDGER_DB,))
    return con


def init_db(con):
    con.executescript("""
    CREATE TABLE IF NOT EXISTS files (
        path    TEXT PRIMARY KEY,
        size    INTEGER NOT NULL,
        mtime   REAL    NOT NULL,
        dev     INTEGER,
        inode   INTEGER,
        phash   TEXT,
        fhash   TEXT,
        deleted INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS ix_files_size  ON files(size);
    CREATE INDEX IF NOT EXISTS ix_files_fhash ON files(fhash);

    CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);

    CREATE TABLE IF NOT EXISTS led.actions (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        batch   TEXT NOT NULL,
        root    TEXT NOT NULL DEFAULT '',
        src     TEXT NOT NULL,
        dst     TEXT NOT NULL,
        size    INTEGER NOT NULL,
        ts      REAL NOT NULL,
        undone  INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS led.ix_actions_batch ON actions(batch, root);
    CREATE INDEX IF NOT EXISTS led.ix_actions_dst   ON actions(dst);
    """)

    # ถ้าเคยใช้เวอร์ชันไฟล์เดียว ย้ายตาราง actions ออกมาไว้ที่ ledger
    if con.execute("SELECT 1 FROM main.sqlite_master WHERE type='table' "
                   "AND name='actions'").fetchone():
        cols = [r[1] for r in con.execute("PRAGMA main.table_info(actions)")]
        root_col = "root" if "root" in cols else "?"
        args = () if "root" in cols else (meta_get(con, "root", "") or "",)
        con.execute("INSERT INTO led.actions(batch,root,src,dst,size,ts,undone) "
                    "SELECT batch,%s,src,dst,size,ts,undone FROM main.actions" % root_col,
                    args)
        con.execute("DROP TABLE main.actions")

    # ledger รุ่นเก่ายังไม่มีคอลัมน์ root
    if "root" not in [r[1] for r in con.execute("PRAGMA led.table_info(actions)")]:
        con.execute("ALTER TABLE led.actions ADD COLUMN root TEXT NOT NULL DEFAULT ''")
        con.execute("UPDATE led.actions SET root=? WHERE root=''",
                    (meta_get(con, "root", "") or "",))
    con.commit()


def meta_get(con, k, default=None):
    row = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row["v"] if row else default


def meta_set(con, k, v):
    con.execute("INSERT INTO meta(k,v) VALUES(?,?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
    con.commit()


# ---------------------------------------------------------------------- hashing

def hash_file(path, limit=None):
    h = hashlib.blake2b(digest_size=16)
    read = 0
    with open(path, "rb", buffering=0) as f:
        while True:
            want = CHUNK if limit is None else min(CHUNK, limit - read)
            if want <= 0:
                break
            block = f.read(want)
            if not block:
                break
            h.update(block)
            read += len(block)
    return h.hexdigest()


# ----------------------------------------------------------------------- scanner

class Scanner(object):
    """Three-pass duplicate detection: size -> head hash -> full hash."""

    def __init__(self, con):
        self.con = con
        self.lock = threading.Lock()
        self.cancel = threading.Event()
        self.thread = None
        self.reset("idle")

    def reset(self, phase):
        self.progress = {
            "phase": phase, "message": "", "done": 0, "total": 0,
            "bytes_done": 0, "bytes_total": 0, "started": 0, "error": None,
        }

    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def set(self, **kw):
        with self.lock:
            self.progress.update(kw)

    def snapshot(self):
        with self.lock:
            return dict(self.progress)

    def start(self, root, min_size, follow_hidden):
        if self.running():
            return False
        self.cancel.clear()
        self.reset("starting")
        self.set(started=time.time(), message="กำลังเตรียม")
        self.thread = threading.Thread(
            target=self._run, args=(root, min_size, follow_hidden), daemon=True)
        self.thread.start()
        return True

    def _run(self, root, min_size, follow_hidden):
        try:
            self._scan(root, min_size, follow_hidden)
            if self.cancel.is_set():
                self.set(phase="cancelled", message="ยกเลิกแล้ว")
            else:
                self.set(phase="done", message="สแกนเสร็จแล้ว")
        except Exception as exc:  # surface it in the UI rather than dying quietly
            self.set(phase="error", error="%s: %s" % (type(exc).__name__, exc))

    # -- pass 0: walk ------------------------------------------------------
    def _walk(self, root, min_size, follow_hidden):
        found = []
        count = 0
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = [d for d in dirnames
                           if d not in SKIP_DIRS
                           and not (d.startswith(".") and not follow_hidden)]
            for name in filenames:
                if name in SKIP_NAMES or name.startswith("._"):
                    continue
                if name.startswith(".") and not follow_hidden:
                    continue
                full = os.path.join(dirpath, name)
                try:
                    st = os.lstat(full)
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue
                if st.st_size < min_size or st.st_size == 0:
                    continue
                found.append((full, st.st_size, st.st_mtime, st.st_dev, st.st_ino))
                count += 1
                if count % 500 == 0:
                    self.set(done=count, message="กำลังไล่: %s" % dirpath[-70:])
                    if self.cancel.is_set():
                        return found
        return found

    def _scan(self, root, min_size, follow_hidden):
        con = self.con
        self.set(phase="walk", message="กำลังไล่รายชื่อไฟล์", done=0, total=0)
        found = self._walk(root, min_size, follow_hidden)
        if self.cancel.is_set():
            return
        self.set(phase="index", total=len(found), done=0,
                 message="ทำดัชนี %d ไฟล์" % len(found))

        # keep cached hashes for files whose size+mtime are unchanged
        cached = {}
        for row in con.execute("SELECT path,size,mtime,phash,fhash FROM files"):
            cached[row["path"]] = (row["size"], row["mtime"], row["phash"], row["fhash"])

        con.execute("DELETE FROM files")
        rows = []
        for full, size, mtime, dev, ino in found:
            old = cached.get(full)
            ph = fh = None
            if old and old[0] == size and abs(old[1] - mtime) < 0.001:
                ph, fh = old[2], old[3]
            rows.append((full, size, mtime, dev, ino, ph, fh))
        con.executemany(
            "INSERT INTO files(path,size,mtime,dev,inode,phash,fhash,deleted) "
            "VALUES(?,?,?,?,?,?,?,0)", rows)
        con.commit()

        # -- pass 1: size buckets -----------------------------------------
        self.set(phase="size", message="จัดกลุ่มตามขนาดไฟล์")
        sizes = [r["size"] for r in con.execute(
            "SELECT size FROM files GROUP BY size HAVING COUNT(*)>1")]
        if not sizes:
            self.set(phase="done", message="ไม่มีไฟล์ขนาดชนกันเลย — ไม่มีอะไรซ้ำ")
            return

        candidates = []
        for chunk in _chunks(sizes, 400):
            q = "SELECT path,size,dev,inode,phash,fhash FROM files WHERE size IN (%s)" % \
                ",".join("?" * len(chunk))
            candidates.extend(con.execute(q, chunk).fetchall())

        # drop hardlinks: same (dev,inode) is one physical file, not a duplicate
        seen_inode = set()
        pool = []
        for r in candidates:
            key = (r["dev"], r["inode"])
            if r["inode"] and key in seen_inode:
                continue
            if r["inode"]:
                seen_inode.add(key)
            pool.append(r)

        # -- pass 2: head hash --------------------------------------------
        todo = [r for r in pool if r["phash"] is None]
        self.set(phase="head", total=len(todo), done=0,
                 message="hash หัวไฟล์ %d รายการ" % len(todo))
        self._hash_many(todo, "phash", limit=PARTIAL_BYTES)
        if self.cancel.is_set():
            return

        # -- pass 3: full hash --------------------------------------------
        self.set(phase="full", message="คัดรายการที่ต้อง hash เต็มไฟล์")
        dup_keys = set()
        for r in con.execute(
                "SELECT size,phash FROM files WHERE phash IS NOT NULL "
                "GROUP BY size,phash HAVING COUNT(*)>1"):
            dup_keys.add((r["size"], r["phash"]))

        need = []
        for r in pool:
            row = con.execute("SELECT path,size,phash,fhash FROM files WHERE path=?",
                              (r["path"],)).fetchone()
            if row and row["fhash"] is None and (row["size"], row["phash"]) in dup_keys:
                need.append(row)

        total_bytes = sum(r["size"] for r in need)
        self.set(phase="full", total=len(need), done=0, bytes_done=0,
                 bytes_total=total_bytes,
                 message="hash เต็มไฟล์ %d รายการ (%s)" % (len(need), human(total_bytes)))
        self._hash_many(need, "fhash", limit=None, count_bytes=True)
        if self.cancel.is_set():
            return

        # files that ended up alone keep their hash but form no group - fine
        meta_set(con, "root", root)
        meta_set(con, "min_size", min_size)
        meta_set(con, "last_scan", time.time())

    def _hash_many(self, rows, column, limit, count_bytes=False):
        con = self.con
        rows = sorted(rows, key=lambda r: r["path"])
        buf = []
        done = 0
        bytes_done = 0
        for r in rows:
            if self.cancel.is_set():
                break
            path = r["path"]
            try:
                digest = hash_file(path, limit)
            except (OSError, IOError):
                digest = None
            buf.append((digest, path))
            done += 1
            if count_bytes:
                bytes_done += r["size"]
            if len(buf) >= 200:
                con.executemany("UPDATE files SET %s=? WHERE path=?" % column, buf)
                con.commit()
                buf = []
            if done % 20 == 0 or count_bytes:
                self.set(done=done, bytes_done=bytes_done,
                         message=os.path.basename(path)[:70])
        if buf:
            con.executemany("UPDATE files SET %s=? WHERE path=?" % column, buf)
            con.commit()
        self.set(done=done, bytes_done=bytes_done)


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%.0f %s" % (n, unit)) if unit == "B" else ("%.2f %s" % (n, unit))
        n /= 1024.0


# ------------------------------------------------------------------- quarantine

def quarantine_root(root):
    return os.path.join(root, QUARANTINE_NAME)


def batch_dir(root, batch):
    return os.path.join(quarantine_root(root), batch)


# ---- manifest: สำเนาบัญชีที่เดินทางไปพร้อมไฟล์เสมอ ------------------------

def write_manifest(bdir, batch, root, entries):
    """เขียน manifest.json ไว้ในโฟลเดอร์ชุดนั้นเอง

    ถ้า ledger หายไป (โดนลบ, ย้ายเครื่อง, ก็อปไดรฟ์) ไฟล์พวกนี้ยังบอกตัวเองได้ว่า
    เคยอยู่ตรงไหน เลยดึงกลับเข้าระบบได้เสมอ
    """
    data = {"tool": "dupfinder", "version": 1, "batch": batch, "root": root,
            "updated": time.time(), "entries": entries}
    tmp = os.path.join(bdir, MANIFEST_NAME + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, os.path.join(bdir, MANIFEST_NAME))
    except (OSError, IOError):
        pass


def read_manifest(bdir):
    try:
        with open(os.path.join(bdir, MANIFEST_NAME), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) and data.get("tool") == "dupfinder" else None
    except Exception:
        return None


def sync_manifest(con, batch, root):
    bdir = batch_dir(root, batch)
    if not os.path.isdir(bdir):
        return
    entries = []
    for r in con.execute("SELECT src,dst,size,undone FROM led.actions "
                         "WHERE batch=? AND root=? ORDER BY id", (batch, root)):
        entries.append({
            "rel": os.path.relpath(r["dst"], bdir),
            "src_rel": os.path.relpath(r["src"], root),
            "size": r["size"],
            "undone": bool(r["undone"]),
        })
    if all(e["undone"] for e in entries):
        try:
            os.remove(os.path.join(bdir, MANIFEST_NAME))
        except OSError:
            pass
        return
    write_manifest(bdir, batch, root, entries)


# ---- ย้าย / คืน / ลบถาวร -------------------------------------------------

def move_to_quarantine(con, paths):
    """ย้ายไฟล์ไป <root>/_DUPFINDER_QUARANTINE/<วันเวลา>/<path เดิม>"""
    root = meta_get(con, "root") or "/"
    batch = time.strftime("%Y-%m-%d_%H%M%S")
    base = batch_dir(root, batch)
    moved, failed, freed = [], [], 0

    for path in paths:
        try:
            size = os.path.getsize(path)
        except OSError:
            failed.append({"path": path, "error": "ไม่พบไฟล์"})
            continue
        rel = os.path.relpath(path, root)
        if rel.startswith(".."):                       # ไฟล์อยู่นอก root
            rel = os.path.splitdrive(path)[1].lstrip("/\\") or os.path.basename(path)
        dst = os.path.join(base, rel)
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            dst = _unique(dst)
            try:
                os.rename(path, dst)          # ในไดรฟ์เดียวกัน = ขยับ metadata เฉยๆ
            except OSError:
                shutil.move(path, dst)
        except (OSError, IOError) as exc:
            failed.append({"path": path, "error": str(exc)})
            continue
        con.execute("INSERT INTO led.actions(batch,root,src,dst,size,ts) "
                    "VALUES(?,?,?,?,?,?)", (batch, root, path, dst, size, time.time()))
        con.execute("UPDATE files SET deleted=1 WHERE path=?", (path,))
        moved.append(path)
        freed += size
    con.commit()
    if moved:
        sync_manifest(con, batch, root)
    return {"batch": batch, "root": root, "moved": len(moved), "failed": failed,
            "freed": freed, "freed_h": human(freed), "dir": base}


def resolve_root(con, batch, root):
    """เติม root ให้เองถ้าชื่อชุดไม่กำกวม

    UI เก่าที่ค้างอยู่ในแคชจะไม่ส่ง root มา เมื่อก่อนเลยไปตรงกับ 0 แถว
    แล้วรายงานว่าสำเร็จทั้งที่ไม่ได้ทำอะไร
    """
    if root and con.execute("SELECT 1 FROM led.actions WHERE batch=? AND root=?",
                            (batch, root)).fetchone():
        return root, None
    roots = [r["root"] for r in con.execute(
        "SELECT DISTINCT root FROM led.actions WHERE batch=?", (batch,))]
    if not roots:
        return None, {"error": "ไม่พบชุด %s ในบัญชีถังพักไฟล์" % batch}
    if len(roots) > 1:
        return None, {"error": "ชื่อชุด %s ซ้ำกันหลาย root — ต้องระบุ root ให้ชัด" % batch}
    return roots[0], None


def undo_batch(con, batch, root, only=None):
    """คืนทั้งชุด หรือเฉพาะ path ในถังพักที่ระบุใน `only`"""
    root, err = resolve_root(con, batch, root)
    if err:
        return dict(err, restored=0, failed=[])
    restored, failed = 0, []
    rows = con.execute("SELECT * FROM led.actions WHERE batch=? AND root=? AND undone=0",
                       (batch, root)).fetchall()
    if only:
        keep = set(only)
        rows = [r for r in rows if r["dst"] in keep]
    for r in rows:
        if not os.path.exists(r["dst"]):
            failed.append({"path": r["dst"], "error": "หายไปจากถังพักไฟล์"})
            continue
        try:
            os.makedirs(os.path.dirname(r["src"]), exist_ok=True)
            dst = _unique(r["src"])
            try:
                os.rename(r["dst"], dst)
            except OSError:
                shutil.move(r["dst"], dst)
        except (OSError, IOError) as exc:
            failed.append({"path": r["dst"], "error": str(exc)})
            continue
        con.execute("UPDATE led.actions SET undone=1 WHERE id=?", (r["id"],))
        con.execute("UPDATE files SET deleted=0 WHERE path=?", (r["src"],))
        restored += 1
    con.commit()
    sync_manifest(con, batch, root)
    _prune_empty(batch_dir(root, batch))
    return {"restored": restored, "failed": failed}


def purge_batch(con, batch, root):
    """ลบถาวร — จะไม่ยอมลบแถวในบัญชีถ้าหาโฟลเดอร์จริงไม่เจอ

    เดิมใช้ root ปัจจุบันคำนวณ path ถ้าสลับไปสแกนไดรฟ์อื่นแล้วกดลบ
    มันจะลบบัญชีทิ้งโดยที่ไฟล์จริงยังค้างอยู่บนไดรฟ์เดิมแบบไร้เจ้าของ
    """
    root, err = resolve_root(con, batch, root)
    if err:
        return err
    base = batch_dir(root, batch)
    alive = con.execute("SELECT COUNT(*) n FROM led.actions "
                        "WHERE batch=? AND root=? AND undone=0",
                        (batch, root)).fetchone()["n"]
    if alive and not os.path.isdir(base):
        return {"error": "ไม่พบโฟลเดอร์ %s — ไดรฟ์อาจไม่ได้เสียบอยู่ "
                         "ยังไม่ได้ลบอะไรทั้งนั้น" % base}
    if os.path.isdir(base):
        shutil.rmtree(base, ignore_errors=True)
        if os.path.isdir(base):
            return {"error": "ลบโฟลเดอร์ %s ไม่สำเร็จ" % base}
    con.execute("DELETE FROM led.actions WHERE batch=? AND root=?", (batch, root))
    con.commit()
    _prune_empty(quarantine_root(root))
    return {"purged": batch}


def _unique(path):
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    i = 2
    while os.path.exists("%s (%d)%s" % (stem, i, ext)):
        i += 1
    return "%s (%d)%s" % (stem, i, ext)


def _prune_empty(base):
    """เก็บกวาดโฟลเดอร์เปล่าจากล่างขึ้นบน

    เช็คด้วย os.listdir ตอนไปถึงจริงๆ ไม่ใช่รายชื่อที่ os.walk เก็บไว้ตั้งแต่ต้น
    ไม่งั้นโฟลเดอร์แม่จะยังนึกว่าตัวเองมีลูกอยู่ ทั้งที่เพิ่งลบลูกไปเมื่อกี้
    """
    for dirpath, dirnames, filenames in os.walk(base, topdown=False):
        try:
            if not os.listdir(dirpath):
                os.rmdir(dirpath)
        except OSError:
            pass
    try:                                   # เก็บ _DUPFINDER_QUARANTINE ด้วยถ้าว่างแล้ว
        parent = os.path.dirname(base.rstrip("/\\"))
        if os.path.basename(parent) == QUARANTINE_NAME and not os.listdir(parent):
            os.rmdir(parent)
    except OSError:
        pass


# ---- ตามหาถังพักที่ค้างอยู่ แล้วดึงกลับเข้าระบบ ---------------------------

def find_quarantines(base, max_depth=6):
    base = os.path.abspath(os.path.expanduser(base or "/"))
    if not os.path.isdir(base):
        return []
    found = []
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        if QUARANTINE_NAME in dirnames:
            found.append(os.path.join(dirpath, QUARANTINE_NAME))
            dirnames.remove(QUARANTINE_NAME)
        rel = os.path.relpath(dirpath, base)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        if depth >= max_depth:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames
                       if d not in SKIP_DIRS and not d.startswith(".")]
    return found


def _batch_files(bdir):
    out = []
    for dirpath, dirnames, filenames in os.walk(bdir):
        for name in filenames:
            if name == MANIFEST_NAME or name.startswith("._"):
                continue
            full = os.path.join(dirpath, name)
            try:
                out.append((full, os.path.getsize(full)))
            except OSError:
                continue
    return out


def survey_quarantines(con, base):
    """รายงานว่ามีชุดไหนค้างอยู่บ้าง และชุดไหนที่ระบบยังไม่รู้จัก"""
    report = []
    for qdir in find_quarantines(base):
        root = os.path.dirname(qdir)
        try:
            names = sorted(os.listdir(qdir))
        except OSError:
            continue
        for name in names:
            bdir = os.path.join(qdir, name)
            if not os.path.isdir(bdir):
                continue
            files = _batch_files(bdir)
            if not files:
                continue
            known = con.execute(
                "SELECT COUNT(*) n FROM led.actions WHERE batch=? AND root=? AND undone=0",
                (name, root)).fetchone()["n"]
            report.append({
                "dir": bdir, "root": root, "batch": name,
                "files": len(files), "bytes": sum(sz for _, sz in files),
                "bytes_h": human(sum(sz for _, sz in files)),
                "manifest": read_manifest(bdir) is not None,
                "known": known, "orphan": known == 0,
            })
    report.sort(key=lambda r: (r["root"], r["batch"]))
    return report


def adopt_batch(con, bdir):
    """ดึงชุดที่ค้างอยู่กลับเข้าบัญชี

    ใช้ manifest.json ถ้ามี ถ้าไม่มีก็ยังกู้ได้ เพราะโครงสร้างโฟลเดอร์ในถังพัก
    สะท้อน path เดิมอยู่แล้ว (<batch>/<path ที่สัมพันธ์กับ root>)
    """
    bdir = os.path.abspath(bdir)
    qdir = os.path.dirname(bdir)
    if os.path.basename(qdir) != QUARANTINE_NAME or not os.path.isdir(bdir):
        return {"error": "ไม่ใช่โฟลเดอร์ชุดในถังพักไฟล์: %s" % bdir}
    root = os.path.dirname(qdir)
    batch = os.path.basename(bdir)

    man = read_manifest(bdir)
    src_by_rel = {}
    if man:
        for e in man.get("entries") or []:
            if isinstance(e, dict) and not e.get("undone") and e.get("rel"):
                src_by_rel[e["rel"]] = os.path.join(
                    root, e.get("src_rel") or e["rel"])

    added, skipped = 0, 0
    for full, size in _batch_files(bdir):
        if con.execute("SELECT 1 FROM led.actions WHERE dst=?", (full,)).fetchone():
            skipped += 1
            continue
        rel = os.path.relpath(full, bdir)
        src = src_by_rel.get(rel) or os.path.join(root, rel)
        try:
            ts = os.path.getmtime(full)
        except OSError:
            ts = time.time()
        con.execute("INSERT INTO led.actions(batch,root,src,dst,size,ts) "
                    "VALUES(?,?,?,?,?,?)", (batch, root, src, full, size, ts))
        added += 1
    con.commit()
    if added:
        sync_manifest(con, batch, root)
    return {"batch": batch, "root": root, "added": added, "skipped": skipped,
            "manifest": man is not None}


# ---------------------------------------------------------------------- queries

def list_groups(con, offset=0, limit=40, sort="waste", query=""):
    where = "fhash IS NOT NULL AND deleted=0"
    params = []
    if query:
        where += (" AND fhash IN (SELECT fhash FROM files "
                  "WHERE deleted=0 AND path LIKE ? ESCAPE '\\')")
        params.append("%" + query.replace("\\", "\\\\").replace("%", "\\%") + "%")

    order = {
        "waste": "size*(c-1) DESC",
        "size": "size DESC",
        "count": "c DESC, size DESC",
        "path": "sample ASC",
    }.get(sort, "size*(c-1) DESC")

    base = ("SELECT fhash, size, COUNT(*) c, MIN(path) sample FROM files "
            "WHERE %s GROUP BY fhash, size HAVING c>1" % where)

    total = con.execute("SELECT COUNT(*) n FROM (%s)" % base, params).fetchone()["n"]
    stats = con.execute(
        "SELECT COALESCE(SUM(size*(c-1)),0) waste, COALESCE(SUM(c-1),0) extra "
        "FROM (%s)" % base, params).fetchone()

    rows = con.execute("%s ORDER BY %s LIMIT ? OFFSET ?" % (base, order),
                       params + [limit, offset]).fetchall()
    groups = []
    for g in rows:
        files = con.execute(
            "SELECT path,size,mtime FROM files WHERE fhash=? AND size=? AND deleted=0 "
            "ORDER BY mtime ASC", (g["fhash"], g["size"])).fetchall()
        groups.append({
            "hash": g["fhash"][:12],
            "size": g["size"],
            "size_h": human(g["size"]),
            "waste": g["size"] * (g["c"] - 1),
            "waste_h": human(g["size"] * (g["c"] - 1)),
            "files": [{"path": f["path"], "mtime": f["mtime"],
                       "thumb": os.path.splitext(f["path"])[1].lower() in THUMBABLE}
                      for f in files],
        })
    return {"total_groups": total, "groups": groups,
            "waste": stats["waste"], "waste_h": human(stats["waste"]),
            "extra_files": stats["extra"]}


# ------------------------------------------------------------- folder browsing

def _mount_points():
    """ไดรฟ์ที่ต่ออยู่ — ตำแหน่งต่างกันไปตาม OS"""
    found = []
    if IS_WIN:
        import string
        for letter in string.ascii_uppercase:
            drive = "%s:\\" % letter
            if os.path.isdir(drive):
                found.append((drive, drive))
    elif IS_MAC:
        for name in sorted(_safe_listdir("/Volumes")):
            full = os.path.join("/Volumes", name)
            if os.path.isdir(full):
                found.append((name, full))
    else:                                   # Linux / BSD
        user = os.path.basename(os.path.expanduser("~"))
        seen = set()
        for base in ("/media/%s" % user, "/run/media/%s" % user, "/media", "/mnt"):
            for name in sorted(_safe_listdir(base)):
                full = os.path.join(base, name)
                if os.path.isdir(full) and full not in seen:
                    seen.add(full)
                    found.append((name, full))
    return found


def _safe_listdir(path):
    try:
        return os.listdir(path)
    except OSError:
        return []


def list_places():
    """ทางลัดในตัวเลือกโฟลเดอร์: ไดรฟ์ที่ต่ออยู่ + โฟลเดอร์ประจำในบ้าน"""
    out = []
    for name, full in _mount_points():
        out.append({"name": name, "path": os.path.realpath(full), "kind": "volume"})
    home = os.path.expanduser("~")
    out.append({"name": os.path.basename(home) or home, "path": home, "kind": "home"})
    for name in ("Desktop", "Documents", "Downloads", "Pictures",
                 "Movies", "Videos", "Music"):
        full = os.path.join(home, name)
        if os.path.isdir(full):
            out.append({"name": name, "path": full, "kind": "folder"})
    return out


def _crumbs(path):
    """แตก path เป็นชิ้นๆ ด้วย os.path.split เพื่อให้ใช้ได้ทั้ง / และ \\"""
    parts, head = [], path
    while True:
        head, tail = os.path.split(head)
        if not tail:
            if head:
                parts.append({"name": head, "path": head})
            break
        parts.append({"name": tail, "path": os.path.join(head, tail)})
    parts.reverse()
    return parts


def browse_dir(path, show_hidden=False):
    """List sub-folders of `path`. Directories only - this picks a scan root."""
    path = os.path.abspath(os.path.expanduser(path or os.path.expanduser("~")))
    if not os.path.isdir(path):
        return {"error": "ไม่ใช่โฟลเดอร์: %s" % path}
    dirs, files, denied = [], 0, False
    try:
        for name in os.listdir(path):
            if name.startswith(".") and not show_hidden:
                continue
            full = os.path.join(path, name)
            try:
                if os.path.islink(full):
                    continue
                if os.path.isdir(full):
                    dirs.append({"name": name, "path": full,
                                 "skipped": name in SKIP_DIRS})
                else:
                    files += 1
            except OSError:
                continue
    except PermissionError:
        denied = True
    except OSError as exc:
        return {"error": str(exc)}

    dirs.sort(key=lambda d: d["name"].lower())
    parent = os.path.dirname(path.rstrip("/\\")) or path
    return {"path": path, "parent": parent if parent != path else None,
            "crumbs": _crumbs(path),
            "dirs": dirs, "files": files, "denied": denied}


# ------------------------------------------------------------------ thumbnails

def _thumb_quicklook(path, out):
    """macOS: ใช้ QuickLook ซึ่งรู้จัก HEIC, MOV, RAW, PDF อยู่แล้ว"""
    tmp = out + ".d"
    os.makedirs(tmp, exist_ok=True)
    try:
        subprocess.run(["qlmanage", "-t", "-s", "320", "-o", tmp, path],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=25)
        made = [f for f in os.listdir(tmp) if f.endswith(".png")]
        if not made:
            return False
        shutil.move(os.path.join(tmp, made[0]), out)
        return True
    except Exception:
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _thumb_pillow(path, out):
    """ทางเลือกสำหรับ Linux/Windows — ใช้ก็ต่อเมื่อเครื่องมี Pillow ติดตั้งอยู่

    เป็น optional ล้วนๆ ไม่มี Pillow ก็ยังใช้โปรแกรมได้ครบ แค่ไม่มีรูปตัวอย่าง
    """
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return False
    try:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            im.thumbnail((320, 320))
            im.convert("RGB").save(out, "PNG")
        return True
    except Exception:
        try:
            os.remove(out)
        except OSError:
            pass
        return False


def make_thumb(path):
    key = hashlib.blake2b(path.encode("utf-8", "replace"), digest_size=12).hexdigest()
    os.makedirs(THUMB_DIR, exist_ok=True)
    out = os.path.join(THUMB_DIR, key + ".png")
    if os.path.exists(out):
        return out
    if IS_MAC and _thumb_quicklook(path, out):
        return out
    if _thumb_pillow(path, out):
        return out
    return None


def reveal_in_file_manager(path):
    """เปิดตำแหน่งไฟล์ในโปรแกรมจัดการไฟล์ของแต่ละ OS"""
    try:
        if IS_MAC:
            subprocess.Popen(["open", "-R", path])
        elif IS_WIN:
            subprocess.Popen(["explorer", "/select,%s" % path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path)])
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):
    server_version = "dupfinder"

    def log_message(self, fmt, *args):
        pass

    # -- helpers
    def _json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            return {}

    def _known_path(self, path):
        """Only ever read files this tool knows about - no arbitrary filesystem access.

        That means files the scan indexed, plus anything sitting in quarantine
        (those left the `files` table when they were moved).
        """
        con = self.server.con
        if con.execute("SELECT 1 FROM files WHERE path=?", (path,)).fetchone():
            return True
        return con.execute("SELECT 1 FROM led.actions WHERE dst=? OR src=?",
                           (path, path)).fetchone() is not None

    def _send_file(self, path, ctype=None, inline=True, cache=True):
        try:
            size = os.path.getsize(path)
            f = open(path, "rb")
        except OSError:
            self.send_error(404)
            return
        with f:
            self.send_response(200)
            self.send_header("Content-Type",
                             ctype or mimetypes.guess_type(path)[0]
                             or "application/octet-stream")
            self.send_header("Content-Length", str(size))
            # หน้าเว็บห้ามแคช ไม่งั้นเบราว์เซอร์จะค้างอยู่กับ UI เวอร์ชันเก่า
            # แล้วยิง payload คนละแบบกับเซิร์ฟเวอร์ใหม่ (เคยทำให้ปุ่มลบเงียบสนิท)
            self.send_header("Cache-Control",
                             "max-age=3600" if cache else "no-store, must-revalidate")
            if not inline:
                self.send_header("Content-Disposition", "attachment")
            self.end_headers()
            shutil.copyfileobj(f, self.wfile, CHUNK)

    # -- routing
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        con = self.server.con
        one = lambda k, d="": (q.get(k) or [d])[0]

        try:
            if u.path in ("/", "/index.html"):
                self._send_file(os.path.join(HERE, "index.html"),
                                "text/html; charset=utf-8", cache=False)

            elif u.path == "/api/state":
                sc = self.server.scanner
                self._json({
                    "running": sc.running(),
                    "progress": sc.snapshot(),
                    "root": meta_get(con, "root", self.server.default_root),
                    "min_size": int(meta_get(con, "min_size", 4096) or 4096),
                    "last_scan": float(meta_get(con, "last_scan", 0) or 0),
                    "indexed": con.execute(
                        "SELECT COUNT(*) n FROM files").fetchone()["n"],
                })

            elif u.path == "/api/groups":
                self._json(list_groups(
                    con,
                    offset=int(one("offset", "0")),
                    limit=min(200, int(one("limit", "40"))),
                    sort=one("sort", "waste"),
                    query=one("q", "")))

            elif u.path == "/api/batches":
                rows = con.execute(
                    "SELECT batch, root, COUNT(*) n, SUM(size) bytes, MIN(ts) ts, "
                    "SUM(undone) undone FROM led.actions GROUP BY batch, root "
                    "ORDER BY ts DESC").fetchall()
                self._json([{
                    "batch": r["batch"], "root": r["root"], "files": r["n"],
                    "bytes": r["bytes"], "bytes_h": human(r["bytes"]),
                    "ts": r["ts"], "undone": r["undone"],
                    "available": os.path.isdir(batch_dir(r["root"], r["batch"])),
                } for r in rows])

            elif u.path == "/api/batch":
                rows = con.execute(
                    "SELECT src,dst,size,undone,ts FROM led.actions "
                    "WHERE batch=? AND root=? ORDER BY size DESC",
                    (one("batch"), one("root"))).fetchall()
                self._json([{
                    "src": r["src"], "dst": r["dst"], "size": r["size"],
                    "size_h": human(r["size"]), "undone": r["undone"], "ts": r["ts"],
                    "thumb": os.path.splitext(r["src"])[1].lower() in THUMBABLE,
                    "exists": os.path.exists(r["dst"]),
                } for r in rows])

            elif u.path == "/api/places":
                self._json(list_places())

            elif u.path == "/api/browse":
                self._json(browse_dir(one("path"), one("hidden") == "1"))

            elif u.path == "/api/find_quarantine":
                self._json(survey_quarantines(con, one("path") or
                                              meta_get(con, "root", "/")))

            elif u.path == "/api/thumb":
                path = one("path")
                if not self._known_path(path):
                    self.send_error(403)
                    return
                thumb = make_thumb(path)
                if thumb:
                    self._send_file(thumb, "image/png")
                else:
                    self.send_error(404)

            elif u.path == "/api/file":
                path = one("path")
                if not self._known_path(path):
                    self.send_error(403)
                    return
                self._send_file(path)

            elif u.path == "/api/reveal":
                path = one("path")
                if not self._known_path(path):
                    self.send_error(403)
                    return
                self._json({"ok": reveal_in_file_manager(path)})

            else:
                self.send_error(404)
        except BrokenPipeError:
            pass
        except Exception as exc:
            try:
                self._json({"error": "%s: %s" % (type(exc).__name__, exc)}, 500)
            except Exception:
                pass

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        con = self.server.con
        body = self._body()
        try:
            if u.path == "/api/scan":
                root = os.path.abspath(os.path.expanduser(body.get("root") or ""))
                if not os.path.isdir(root):
                    self._json({"error": "ไม่ใช่โฟลเดอร์: %s" % root}, 400)
                    return
                ok = self.server.scanner.start(
                    root, int(body.get("min_size") or 0),
                    bool(body.get("hidden")))
                meta_set(con, "root", root)
                self._json({"started": ok})

            elif u.path == "/api/cancel":
                self.server.scanner.cancel.set()
                self._json({"ok": True})

            elif u.path == "/api/quarantine":
                paths = body.get("paths") or []
                bad = self._would_empty_a_group(con, paths)
                if bad:
                    self._json({"error": "ยกเลิก: มี %d กลุ่มที่จะไม่เหลือไฟล์เลยสัก"
                                         "ไฟล์" % len(bad), "groups": bad[:5]}, 400)
                    return
                self._json(move_to_quarantine(con, paths))

            elif u.path == "/api/undo":
                self._json(undo_batch(con, body.get("batch") or "",
                                      body.get("root") or "",
                                      body.get("paths")))

            elif u.path == "/api/purge":
                self._json(purge_batch(con, body.get("batch") or "",
                                       body.get("root") or ""))

            elif u.path == "/api/adopt":
                results = [adopt_batch(con, d) for d in (body.get("dirs") or [])]
                self._json({"results": results,
                            "added": sum(r.get("added", 0) for r in results)})

            elif u.path == "/api/reset":
                con.execute("DELETE FROM files")      # ดัชนีเท่านั้น ไม่แตะ led.actions
                con.commit()
                self._json({"ok": True})

            else:
                self.send_error(404)
        except BrokenPipeError:
            pass
        except Exception as exc:
            try:
                self._json({"error": "%s: %s" % (type(exc).__name__, exc)}, 500)
            except Exception:
                pass

    def _would_empty_a_group(self, con, paths):
        """Hard stop: every duplicate group must keep at least one file."""
        if not paths:
            return []
        sel = set(paths)
        groups = {}
        for p in paths:
            row = con.execute("SELECT fhash,size FROM files WHERE path=?", (p,)).fetchone()
            if row and row["fhash"]:
                groups[(row["fhash"], row["size"])] = True
        emptied = []
        for (fh, size) in groups:
            alive = con.execute(
                "SELECT path FROM files WHERE fhash=? AND size=? AND deleted=0",
                (fh, size)).fetchall()
            if all(r["path"] in sel for r in alive):
                emptied.append(fh[:12])
        return emptied


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8777, help="พอร์ต (ค่าเริ่มต้น 8777)")
    ap.add_argument("--root", default="",
                    help="โฟลเดอร์เริ่มต้นในช่องสแกน (ค่าเริ่มต้น: โฟลเดอร์บ้าน)")
    ap.add_argument("--no-open", action="store_true",
                    help="ไม่ต้องเปิดเบราว์เซอร์ให้อัตโนมัติ")
    args = ap.parse_args()

    con = connect()
    init_db(con)

    # ค่าเริ่มต้นคือโฟลเดอร์บ้าน — ไม่เดาแทนผู้ใช้ว่าจะสแกนไดรฟ์ไหน
    default_root = (args.root or meta_get(con, "root", "")
                    or os.path.expanduser("~"))
    default_root = os.path.abspath(os.path.expanduser(default_root))

    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    httpd.con = con
    httpd.scanner = Scanner(con)
    httpd.default_root = default_root

    url = "http://127.0.0.1:%d" % args.port
    # flush ทุกบรรทัด ไม่งั้นตอน redirect หรือรันเป็น service จะไม่เห็นอะไรเลย
    for line in ("dupfinder เปิดที่ %s" % url,
                 "โฟลเดอร์เริ่มต้น: %s" % default_root,
                 "ถังพักไฟล์:       <โฟลเดอร์ที่สแกน>/%s" % QUARANTINE_NAME,
                 "บัญชีถังพักไฟล์:  %s  (ห้ามลบ)" % LEDGER_DB,
                 "กด Ctrl-C เพื่อปิด"):
        print(line, flush=True)
    if not args.no_open:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nปิดแล้ว", flush=True)


if __name__ == "__main__":
    main()
