"""State DB and file operations on Library/ and Archive/.

Invariants
  * Files are only ever moved or copied into Archive/, never deleted.
  * A file's Library path is fixed when first added. Date changes only rewrite
    its .xmp sidecar (and mtime); moving it would make Immich treat it as a new
    photo and drop edits, faces and album membership.
  * A sidecar whose hash differs from the one we wrote was edited by the user in
    Immich: its date becomes a manual override and we never rewrite it.
  * A file that vanishes from Library/ was deleted by the user in Immich: it is
    marked deleted and never re-added, even though Google still has it.
"""
from __future__ import annotations

import concurrent.futures
import csv
import dataclasses
import datetime as dt
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import zipfile
import zlib
from pathlib import Path

import xmp

log = logging.getLogger("takeout_sync.library")

SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
    key        TEXT PRIMARY KEY,   -- "<size>:<crc32>"
    home       TEXT,               -- path under Library/ while active (NULL once released)
    archive    TEXT,               -- path under Archive/ of the archived copy, if any
    item_ids   TEXT NOT NULL,      -- JSON list of "<google url>#<variant>"
    taken      INTEGER,            -- date currently written to the sidecar (UTC epoch)
    date_how   TEXT,
    xmp_hash   TEXT,               -- sha256 of the sidecar we last wrote
    user_xmp   INTEGER NOT NULL DEFAULT 0,  -- 1 = sidecar edited in Immich; never rewrite
    status     TEXT NOT NULL,      -- active | archived | replaced | deleted
    note       TEXT,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS assets_home ON assets(home) WHERE home IS NOT NULL;
CREATE TABLE IF NOT EXISTS manual (
    key       TEXT PRIMARY KEY,
    ts        INTEGER NOT NULL,
    source    TEXT NOT NULL,       -- immich | csv
    csv_value TEXT,                -- last overrides-CSV value applied, so it applies once
    noted     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id       INTEGER PRIMARY KEY,
    export   TEXT, started TEXT, finished TEXT, summary TEXT
);
"""

OVERRIDES_HEADER = ["path", "date"]   # path under Library/; date "YYYY-MM-DD[ HH:MM[:SS]]" local


class State:
    def __init__(self, db: Path, readonly: bool = False):
        self.db = db
        if readonly and not db.exists():
            self.con = sqlite3.connect(":memory:")
        elif readonly:
            self.con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        else:
            db.parent.mkdir(parents=True, exist_ok=True)
            self.con = sqlite3.connect(db)
        self.con.row_factory = sqlite3.Row
        if not readonly or not db.exists():
            self.con.executescript(SCHEMA)
        self.rows: dict[str, dict] = {r["key"]: dict(r) for r in
                                      self.con.execute("SELECT * FROM assets")}
        self.manual: dict[str, dict] = {r["key"]: dict(r) for r in
                                        self.con.execute("SELECT * FROM manual")}

    def by_status(self, *statuses: str) -> list[dict]:
        return [r for r in self.rows.values() if r["status"] in statuses]

    def save(self, row: dict) -> None:
        self.rows[row["key"]] = row
        cols = list(row)
        self.con.execute(f"INSERT OR REPLACE INTO assets ({','.join(cols)}) VALUES "
                         f"({','.join('?' * len(cols))})", [row[c] for c in cols])

    def set_manual(self, key: str, ts: int, source: str, csv_value: str | None = None) -> None:
        prev = self.manual.get(key, {})
        row = {"key": key, "ts": ts, "source": source,
               "csv_value": csv_value if csv_value is not None else prev.get("csv_value"),
               "noted": dt.datetime.now().isoformat(timespec="seconds")}
        self.manual[key] = row
        self.con.execute("INSERT OR REPLACE INTO manual VALUES (?,?,?,?,?)",
                         [row[c] for c in ("key", "ts", "source", "csv_value", "noted")])

    def commit(self) -> None:
        self.con.commit()


# --- Harvest: what the user did in Immich since the last run -----------------

@dataclasses.dataclass
class Harvest:
    missing: list[str] = dataclasses.field(default_factory=list)      # keys deleted in Immich
    trashed: list[str] = dataclasses.field(default_factory=list)      # keys in Immich trash
    xmp_edits: dict[str, int | None] = dataclasses.field(default_factory=dict)  # key -> date
    csv: dict[str, tuple[int, str]] = dataclasses.field(default_factory=dict)   # key -> (ts, raw)
    warnings: list[str] = dataclasses.field(default_factory=list)


MAX_MISSING_FRACTION = 0.05


def harvest(state: State, base: Path, trashed_paths: list[str] | None) -> Harvest:
    h = Harvest()
    lib = base / "Library"
    active = state.by_status("active")
    by_home = {r["home"]: r for r in active}

    for r in active:
        if not (lib / r["home"]).exists():
            h.missing.append(r["key"])
    if len(h.missing) > max(len(active) * MAX_MISSING_FRACTION, 20):
        raise SystemExit(
            f"{len(h.missing):,} of {len(active):,} Library files are missing (> "
            f"{MAX_MISSING_FRACTION:.0%}). Is {lib} mounted? Refusing to mark them deleted.")

    for p in trashed_paths or []:
        if (r := by_home.get(p)) is not None and not r["archive"]:
            h.trashed.append(r["key"])

    for r in active:
        if r["key"] in h.missing or not r["xmp_hash"] or r["user_xmp"]:
            continue
        side = xmp.sidecar_path(lib / r["home"])
        if xmp.file_digest(side) != r["xmp_hash"]:
            h.xmp_edits[r["key"]] = xmp.read_date(side)
    # Previously detected user sidecars: keep their (possibly re-edited) date current.
    for r in active:
        if r["user_xmp"] and r["key"] not in h.missing:
            ts = xmp.read_date(xmp.sidecar_path(lib / r["home"]))
            if ts is not None and ts != state.manual.get(r["key"], {}).get("ts"):
                h.xmp_edits[r["key"]] = ts

    h.csv = _read_overrides(base / "state" / "date_overrides.csv", by_home, state, h)
    return h


def _read_overrides(path: Path, by_home: dict, state: State, h: Harvest) -> dict:
    if not path.exists():
        return {}
    out = {}
    with open(path, newline="") as f:
        for n, line in enumerate(csv.DictReader(f), 2):
            rel = (line.get("path") or "").strip().removeprefix("Library/")
            raw = (line.get("date") or "").strip()
            if not rel or rel.startswith("#"):
                continue
            r = by_home.get(rel)
            if r is None:
                h.warnings.append(f"date_overrides.csv line {n}: no active photo at {rel}")
                continue
            try:
                ts = int(dt.datetime.fromisoformat(raw).astimezone().timestamp())
            except ValueError:
                h.warnings.append(f"date_overrides.csv line {n}: bad date {raw!r}")
                continue
            if state.manual.get(r["key"], {}).get("csv_value") != raw:
                out[r["key"]] = (ts, raw)
    return out


def apply_harvest(state: State, base: Path, h: Harvest) -> None:
    lib, arc = base / "Library", base / "Archive"
    for key in h.trashed:                       # copy before Immich purges it
        r = state.rows[key]
        dest = unique_path(arc / "deleted-in-immich" / r["home"])
        _copy_with_sidecar(lib / r["home"], dest)
        r["archive"] = str(dest.relative_to(arc))
        r["note"] = "in Immich trash; copied to Archive"
        state.save(r)
    for key in h.missing:
        r = state.rows[key]
        if not r["archive"]:
            # Maybe a previous run crashed between moving the file and saving state.
            for cand in (arc / r["home"], arc / "deleted-in-immich" / r["home"]):
                if cand.exists():
                    r["archive"] = str(cand.relative_to(arc))
        r.update(status="deleted", home=None,
                 note="deleted in Immich" + ("" if r["archive"] else " (no copy kept)"))
        state.save(r)
    for key, ts in h.xmp_edits.items():
        r = state.rows[key]
        r["user_xmp"] = 1
        state.save(r)
        if ts is not None:
            state.set_manual(key, ts, "immich")
    for key, (ts, raw) in h.csv.items():
        state.set_manual(key, ts, "csv", csv_value=raw)
    state.commit()


# --- File operations ---------------------------------------------------------

@dataclasses.dataclass
class AddJob:
    key: str
    part: Path
    member: str | None      # None: loose file, link/copy the whole part
    dest: Path
    size: int
    crc: int
    mtime: int | None


def extract(jobs: list[AddJob], workers: int) -> tuple[list[AddJob], list[tuple[AddJob, str]]]:
    by_part: dict[Path, list[AddJob]] = {}
    for j in jobs:
        by_part.setdefault(j.part, []).append(j)
    done, failed = [], []
    pool_cls = (concurrent.futures.ProcessPoolExecutor if workers > 1
                else concurrent.futures.ThreadPoolExecutor)
    with pool_cls(max(1, workers)) as pool:
        futs = {pool.submit(_extract_part, part, js): js for part, js in by_part.items()}
        for n, fut in enumerate(concurrent.futures.as_completed(futs), 1):
            js = futs[fut]
            try:
                errors = fut.result()
            except Exception as e:  # whole part failed (corrupt zip, disk full ...)
                errors = {j.key: f"{type(e).__name__}: {e}" for j in js}
            for j in js:
                (failed.append((j, errors[j.key])) if j.key in errors else done.append(j))
            log.info("extracted part %d/%d (%s)", n, len(futs), js[0].part.name)
    return done, failed


def _extract_part(part: Path, jobs: list[AddJob]) -> dict[str, str]:
    errors = {}
    if jobs[0].member is None:                     # loose file
        j = jobs[0]
        try:
            j.dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = j.dest.with_name(j.dest.name + ".partial")
            tmp.unlink(missing_ok=True)
            try:
                os.link(part, tmp)                 # same filesystem: instant, no extra space
            except OSError:
                shutil.copyfile(part, tmp)
            if tmp.stat().st_size != j.size:
                raise IOError("size mismatch")
            _finish(tmp, j)
        except Exception as e:
            errors[j.key] = f"{type(e).__name__}: {e}"
        return errors
    with zipfile.ZipFile(part) as zf:
        infos = {i.filename: i for i in zf.infolist()}
        for j in sorted(jobs, key=lambda j: infos[j.member].header_offset):
            try:
                j.dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = j.dest.with_name(j.dest.name + ".partial")
                crc, size = 0, 0
                with zf.open(infos[j.member]) as src, open(tmp, "wb") as out:
                    while chunk := src.read(8 << 20):
                        crc = zlib.crc32(chunk, crc)
                        size += len(chunk)
                        out.write(chunk)
                if (size, crc) != (j.size, j.crc):
                    tmp.unlink(missing_ok=True)
                    raise IOError(f"checksum mismatch ({size}:{crc:08x} != {j.size}:{j.crc:08x})")
                _finish(tmp, j)
            except Exception as e:
                errors[j.key] = f"{type(e).__name__}: {e}"
    return errors


def _finish(tmp: Path, j: AddJob) -> None:
    if j.mtime is not None:
        os.utime(tmp, (j.mtime, j.mtime))
    os.replace(tmp, j.dest)


def write_sidecar(media: Path, data: bytes, mtime: int | None) -> str:
    side = xmp.sidecar_path(media)
    tmp = side.with_name(side.name + ".partial")
    tmp.write_bytes(data)
    os.replace(tmp, side)
    if mtime is not None:  # a changed mtime also makes Immich re-read the metadata
        os.utime(media, (mtime, mtime))
    return xmp.digest(data)


def set_sidecar_date(media: Path, ts: int) -> None:
    """Change only the date in a user-edited sidecar, keeping their other edits."""
    stamp = dt.datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")
    side = xmp.sidecar_path(media)
    subprocess.run(["exiftool", "-q", "-overwrite_original", f"-XMP-exif:DateTimeOriginal={stamp}",
                    f"-XMP-xmp:CreateDate={stamp}", f"-XMP-photoshop:DateCreated={stamp}",
                    str(side)], check=True)
    os.utime(media, (ts, ts))


def move_with_sidecar(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    side = xmp.sidecar_path(src)
    os.replace(src, dest)                  # Library/ and Archive/ share a filesystem
    if side.exists():
        os.replace(side, xmp.sidecar_path(dest))


def _copy_with_sidecar(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    side = xmp.sidecar_path(src)
    if side.exists():
        shutil.copy2(side, xmp.sidecar_path(dest))


def unique_path(p: Path) -> Path:
    if not p.exists():
        return p
    n = 1
    while (q := p.with_name(f"{p.stem}_{n}{p.suffix}")).exists():
        n += 1
    return q


def clean_partials(root: Path) -> int:
    n = 0
    if root.exists():
        for p in root.rglob("*.partial"):
            p.unlink(missing_ok=True)
            n += 1
    return n


def ensure_overrides_template(base: Path) -> None:
    path = base / "state" / "date_overrides.csv"
    if not path.exists():
        with open(path, "w", newline="") as f:
            csv.writer(f).writerow(OVERRIDES_HEADER)


def now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def dumps(x) -> str:
    return json.dumps(sorted(x))
