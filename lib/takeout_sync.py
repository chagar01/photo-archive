#!/usr/bin/env python3
"""Reconcile a Google Photos Takeout snapshot against the organised Library.

Default is a DRY RUN: reports what would be added, replaced, archived, restored
or re-dated, and the only write is the derived embedded-metadata cache
(state/probe_cache.sqlite). With --apply it performs the changes; see
library.py for the invariants (nothing is ever deleted, paths never move,
Immich edits and deletions are respected). Dates: see dates.py.

Identity model
  * Asset   = unique file content, keyed by "<size>:<crc32>" (from the zip
              central directory, so no extraction is needed to diff).
  * Item id = Google's photo URL from the sidecar, plus the file variant
              (extension, -edited). Lets us tell "replaced" (same item, new
              content) from unrelated add + remove.

See ../Takeout/CLAUDE.md for requirements and layout.
"""
from __future__ import annotations

import argparse
import collections
import csv
import dataclasses
import datetime as dt
import fcntl
import json
import logging
import os
import re
import shutil
import sys
import zipfile
import zlib
from pathlib import Path

import dates
import immich
import library
import xmp

DEFAULT_BASE = Path(os.environ.get("BASE_DIR", "/main/GoogleTakeout"))

PHOTOS_ROOT = "Takeout/Google Photos/"
SIDECAR = ".supplemental-metadata"
MEDIA_EXTS = {
    ".jpg", ".jpeg", ".heic", ".heif", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp",
    ".nef", ".cr2", ".cr3", ".arw", ".dng", ".raf", ".orf", ".rw2",
    ".mp4", ".mov", ".avi", ".3gp", ".mpg", ".mpeg", ".wmv", ".m4v", ".mkv", ".webm",
    ".mts", ".m2ts",
}
# Fraction of the library allowed to disappear in one run before we refuse.
MAX_ARCHIVE_FRACTION = 0.05
MAX_ARCHIVE_MIN = 20             # ... and more than this many items (tiny libraries)
FULL_PART_BYTES = 2_000_000_000  # Takeout parts are ~2 GB; a smaller one is the last.

ZIP_RE = re.compile(r"^(?P<export>takeout-\d{8}T\d{6}Z-\d+)-(?P<part>\d{3})\.zip$")
LOOSE_RE = re.compile(r"^(?P<name>.+)-(?P<part>\d{3})(?P<ext>\.[^.]+)$")
DUP_RE = re.compile(r"^(?P<base>.*)\((?P<n>\d+)\)$")

log = logging.getLogger("takeout_sync")


# --- Data -------------------------------------------------------------------

@dataclasses.dataclass
class Source:
    """One occurrence of a media file in the snapshot."""
    part: str              # zip file name, or loose file name
    member: str | None     # path inside zip; None for loose files
    path: str              # logical Takeout path, e.g. "Photos from 2024/IMG_1.HEIC"
    size: int
    crc: int
    sidecar: dict | None = None
    sidecar_how: str = ""  # how the sidecar was matched (exact, dup, edited, stem)
    sniffed_ext: str = ""  # set when the name has no usable extension (e.g. "IMG_2689")

    @property
    def folder(self) -> str:
        return self.path.split("/", 1)[0]

    @property
    def is_year_folder(self) -> bool:
        return self.folder.startswith("Photos from ")


@dataclasses.dataclass
class Asset:
    key: str
    sources: list[Source]
    taken: int | None = None      # UTC epoch seconds, chosen by dates.decide()
    date: dates.Decision | None = None
    item_ids: set[str] = dataclasses.field(default_factory=set)
    albums: set[str] = dataclasses.field(default_factory=set)
    lib_path: str = ""

    @property
    def primary(self) -> Source:
        return self.sources[0]

    @property
    def ext(self) -> str:
        return self.primary.sniffed_ext or os.path.splitext(self.primary.path)[1]

    @property
    def taken_how(self) -> str:
        return self.date.how if self.date else "none"


@dataclasses.dataclass
class Snapshot:
    export: str
    zips: list[Path]
    loose: list[Path]
    parts: dict[int, Path]
    warnings: list[str] = dataclasses.field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not any(w.startswith("INCOMPLETE") for w in self.warnings)


# --- 1. Discover parts ------------------------------------------------------

def photo_exports(takeout_dir: Path) -> tuple[dict[str, list[Path]], list[Path]]:
    """Google Photos exports present ({export id: zips}, oldest first) and loose media parts."""
    exports: dict[str, list[Path]] = collections.defaultdict(list)
    loose: list[Path] = []
    for p in sorted(takeout_dir.iterdir()):
        if not p.is_file() or p.name == "CLAUDE.md":
            continue
        if m := ZIP_RE.match(p.name):
            exports[m["export"]].append(p)
        elif (m := LOOSE_RE.match(p.name)) and m["ext"].lower() in MEDIA_EXTS:
            loose.append(p)
        else:
            log.debug("ignoring %s", p.name)

    # Drop exports with no Google Photos content (e.g. the archive_browser.html-only one).
    photos = {}
    for exp, zips in sorted(exports.items()):          # export ids sort by timestamp
        if any(_has_photos(z) for z in zips):
            photos[exp] = zips
        else:
            log.debug("ignoring non-photos export %s (%d part(s))", exp, len(zips))
    return photos, loose


def index_only_exports(takeout_dir: Path) -> list[Path]:
    """Zips holding nothing but Takeout's archive_browser.html index (one per export)."""
    out = []
    for p in sorted(takeout_dir.iterdir()):
        if re.match(r"^takeout-\d{8}T\d{6}Z-[\d-]+\.zip$", p.name):
            try:
                with zipfile.ZipFile(p) as zf:
                    names = [n for n in zf.namelist() if not n.endswith("/")]
            except (zipfile.BadZipFile, OSError):
                continue
            if names == ["Takeout/archive_browser.html"]:
                out.append(p)
    return out


def discover(takeout_dir: Path) -> Snapshot:
    photos, loose = photo_exports(takeout_dir)
    if not photos:
        sys.exit(f"No Google Photos Takeout zips found in {takeout_dir}")
    if len(photos) > 1 and loose:
        sys.exit(f"Several Photos exports {sorted(photos)} and loose part files "
                 f"{[p.name for p in loose]}: can't tell which export the loose files belong to")
    # Each export is a full snapshot, so applying them oldest first ends in the right state.
    export, zips = next(iter(photos.items()))
    if len(photos) > 1:
        log.info("%d Photos exports present; processing the oldest, %s", len(photos), export)

    parts: dict[int, Path] = {}
    for z in zips:
        parts[int(ZIP_RE.match(z.name)["part"])] = z
    for lf in loose:
        n = int(LOOSE_RE.match(lf.name)["part"])
        if n in parts:
            sys.exit(f"Part {n:03d} present both as {parts[n].name} and {lf.name}")
        parts[n] = lf

    snap = Snapshot(export, zips, loose, parts)
    missing = sorted(set(range(1, max(parts) + 1)) - set(parts))
    if missing:
        snap.warnings.append(f"INCOMPLETE: missing part(s) {', '.join(f'{n:03d}' for n in missing)}")
    last = parts[max(parts)]
    if last.suffix == ".zip" and last.stat().st_size >= FULL_PART_BYTES:
        snap.warnings.append(
            f"INCOMPLETE?: last part {last.name} is full-size; trailing parts may be missing")
    for lf in loose:
        snap.warnings.append(f"loose media file in place of a zip part: {lf.name}")
    return snap


def _has_photos(z: Path) -> bool:
    with zipfile.ZipFile(z) as zf:
        return any(n.startswith(PHOTOS_ROOT) for n in zf.namelist())


def check(takeout_dir: Path) -> list[str]:
    """Problems that make the downloaded Photos export(s) unsafe to rely on: a zip that
    won't open, or a gap in the part numbers. Empty list = OK."""
    problems = []
    zips = sorted(p for p in takeout_dir.iterdir() if ZIP_RE.match(p.name))
    for z in zips:
        try:
            zipfile.ZipFile(z).close()
        except (zipfile.BadZipFile, OSError) as e:
            problems.append(f"{z.name}: not a readable zip ({e})")
    if problems:
        return problems
    photos, loose = photo_exports(takeout_dir)
    for export, parts in photos.items():
        nums = {int(ZIP_RE.match(p.name)["part"]) for p in parts}
        if len(photos) == 1:
            nums |= {int(LOOSE_RE.match(p.name)["part"]) for p in loose}
        missing = sorted(set(range(1, max(nums) + 1)) - nums)
        if missing:
            problems.append(f"{export}: missing part(s) {', '.join(f'{n:03d}' for n in missing)}")
    return problems


# --- 2. Index entries and match sidecars ------------------------------------

def index(snap: Snapshot) -> tuple[list[Source], list[str]]:
    sidecars: dict[str, dict] = {}       # logical path -> parsed JSON
    sources: list[Source] = []
    skipped: list[str] = []

    for z in snap.zips:
        with zipfile.ZipFile(z) as zf:
            for info in zf.infolist():
                if info.is_dir() or not info.filename.startswith(PHOTOS_ROOT):
                    continue
                path = info.filename[len(PHOTOS_ROOT):]
                if path.endswith(".json"):
                    if SIDECAR in os.path.basename(path):
                        try:
                            sidecars[path] = json.loads(zf.read(info))
                        except (ValueError, zipfile.BadZipFile) as e:
                            log.warning("unreadable sidecar %s: %s", path, e)
                    continue
                sniffed = ""
                if os.path.splitext(path)[1].lower() not in MEDIA_EXTS:
                    with zf.open(info) as f:
                        sniffed = _sniff(f.read(16))
                    if not sniffed:
                        skipped.append(path)
                        continue
                sources.append(Source(z.name, info.filename, path, info.file_size, info.CRC,
                                      sniffed_ext=sniffed))

    for lf in snap.loose:
        m = LOOSE_RE.match(lf.name)
        name = m["name"] + m["ext"]
        folder = _folder_for_loose(name, sidecars)
        log.info("checksumming loose file %s (%.1f GB)", lf.name, lf.stat().st_size / 1e9)
        sources.append(Source(lf.name, None, f"{folder}/{name}", lf.stat().st_size, _crc32(lf)))

    for s in sources:
        s.sidecar, s.sidecar_how = _match_sidecar(s.path, sidecars)
    return sources, skipped


def _folder_for_loose(name: str, sidecars: dict) -> str:
    """Loose files lost their folder; recover it from where their sidecar lives."""
    folders = sorted({p.rsplit("/", 1)[0] for p in sidecars
                      if os.path.basename(p) == f"{name}{SIDECAR}.json"})
    year = [f for f in folders if f.startswith("Photos from ")]
    if year or folders:
        return (year or folders)[0]
    return "Loose"


def _sniff(head: bytes) -> str:
    """Extension from magic bytes, for files Google exported without one."""
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"\x89PNG"):
        return ".png"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand == b"qt  ":
            return ".mov"
        if brand in (b"heic", b"heix", b"mif1", b"msf1"):
            return ".heic"
        return ".mp4"
    return ""


def _crc32(path: Path) -> int:
    crc = 0
    with open(path, "rb") as f:
        while chunk := f.read(16 << 20):
            crc = zlib.crc32(chunk, crc)
    return crc


def _match_sidecar(path: str, sidecars: dict) -> tuple[dict | None, str]:
    folder, name = path.rsplit("/", 1)
    stem, ext = os.path.splitext(name)
    m = DUP_RE.match(stem)
    base, dup = (m["base"], f"({m['n']})") if m else (stem, "")
    original = base.removesuffix("-edited")

    candidates = [
        ("exact", f"{name}{SIDECAR}.json"),
        ("dup", f"{base}{ext}{SIDECAR}{dup}.json"),
        ("edited", f"{original}{ext}{SIDECAR}{dup}.json"),
    ]
    # Live Photo videos / RAW+JPEG pairs share the still image's sidecar.
    for e in (".HEIC", ".heic", ".JPG", ".jpg", ".JPEG", ".jpeg", ".NEF", ".nef"):
        if e != ext:
            candidates.append(("stem", f"{original}{e}{SIDECAR}{dup}.json"))
    for how, cand in candidates:
        if (sc := sidecars.get(f"{folder}/{cand}")) is not None:
            return sc, how
    return None, ""


# --- 3. Deduplicate into assets ---------------------------------------------

def build_assets(sources: list[Source]) -> dict[str, Asset]:
    by_key: dict[str, list[Source]] = collections.defaultdict(list)
    for s in sources:
        by_key[f"{s.size}:{s.crc:08x}"].append(s)

    assets = {}
    for key, srcs in by_key.items():
        # Primary source: has a sidecar, lives in a year folder, then stable by path.
        srcs.sort(key=lambda s: (s.sidecar is None, not s.is_year_folder, s.path))
        a = Asset(key, srcs)
        a.albums = {s.folder for s in srcs if not s.is_year_folder}
        for s in srcs:
            if s.sidecar and (url := s.sidecar.get("url")):
                a.item_ids.add(f"{url}#{_variant(s.path)}")
        assets[key] = a
    return assets


def _variant(path: str) -> str:
    stem, ext = os.path.splitext(os.path.basename(path))
    stem = DUP_RE.sub(r"\g<base>", stem)
    return ext.lower() + ("-edited" if stem.endswith("-edited") else "")


def assign_dates(assets: dict[str, Asset], takeout: Path, base: Path, workers: int,
                 probe: bool, manual: dict[str, int] | None = None) -> None:
    tags = {}
    if probe:
        jobs = [(a.key, takeout / a.primary.part, a.primary.member, a.ext) for a in assets.values()]
        tags = dates.probe(jobs, base / "state" / "probe_cache.sqlite", base / "state" / "tmp",
                           workers)
    items = []
    for a in assets.values():
        google = None
        for s in a.sources:
            try:
                # Negative is valid: scanned photos from before 1970.
                google = int(s.sidecar["photoTakenTime"]["timestamp"])
                break
            except (TypeError, KeyError, ValueError):
                pass
        items.append(dates.Item(a.key, os.path.basename(a.primary.path), a.ext, google,
                                tags.get(a.key, {}), sorted(a.albums)))
    for key, d in dates.decide_all(items).items():
        if manual and key in manual:     # the user's correction beats every heuristic
            d = dates.Decision(manual[key], "manual", d.cands, d.rejected, d.hints)
        assets[key].date, assets[key].taken = d, d.ts


def _google_meta(a: Asset) -> tuple[float | None, float | None, str]:
    """GPS and description from Google's sidecar, for our XMP."""
    for s in a.sources:
        sc = s.sidecar or {}
        for geo in (sc.get("geoData"), sc.get("geoDataExif")):
            if geo and (geo.get("latitude") or geo.get("longitude")):
                return geo["latitude"], geo["longitude"], sc.get("description", "")
    desc = next((s.sidecar.get("description", "") for s in a.sources if s.sidecar), "")
    return None, None, desc


def sidecar_bytes(a: Asset) -> bytes:
    lat, lon, desc = _google_meta(a)
    return xmp.build(a.taken, a.date.offset_min if a.date else None, lat, lon, desc)


# --- 4. Diff against state --------------------------------------------------

@dataclasses.dataclass
class Diff:
    added: list[Asset] = dataclasses.field(default_factory=list)
    unchanged: list[Asset] = dataclasses.field(default_factory=list)
    restored: list[Asset] = dataclasses.field(default_factory=list)
    redated: list[tuple[Asset, dict]] = dataclasses.field(default_factory=list)
    replaced: list[tuple[dict, Asset]] = dataclasses.field(default_factory=list)
    archived: list[dict] = dataclasses.field(default_factory=list)
    deleted_skipped: list[Asset] = dataclasses.field(default_factory=list)


def diff(assets: dict[str, Asset], rows: dict[str, dict]) -> Diff:
    d = Diff()
    new_by_item: dict[str, Asset] = {}
    for key, a in assets.items():
        row = rows.get(key)
        if row is None:
            d.added.append(a)
            for i in a.item_ids:
                new_by_item[i] = a
        elif row["status"] == "deleted":
            d.deleted_skipped.append(a)            # user deleted it in Immich: respect that
        elif row["status"] in ("archived", "replaced"):
            d.restored.append(a)
        elif a.taken != row["taken"]:
            d.redated.append((a, row))
        else:
            d.unchanged.append(a)

    replacements = set()
    for key, row in rows.items():
        if row["status"] != "active" or key in assets:
            continue
        repl = next((new_by_item[i] for i in json.loads(row["item_ids"]) if i in new_by_item), None)
        if repl is not None and repl.key not in replacements:
            d.replaced.append((row, repl))
            replacements.add(repl.key)
        else:
            d.archived.append(row)
    d.added = [a for a in d.added if a.key not in replacements]
    return d


# --- 5. Plan library paths --------------------------------------------------

def plan_paths(assets: dict[str, Asset], rows: dict[str, dict], d: Diff) -> None:
    reserved = {r["home"] for r in rows.values() if r["home"]}
    for a in assets.values():
        if (r := rows.get(a.key)) and r["status"] == "active":
            a.lib_path = r["home"]                 # fixed forever
    # A replacement takes over the old file's path (same extension), so Immich sees an
    # updated photo rather than a deleted one plus a new one.
    for old, a in d.replaced:
        if os.path.splitext(old["home"])[1].lower() == a.ext.lower():
            a.lib_path = old["home"]
    for a in sorted(d.added + d.restored + [a for _, a in d.replaced if not a.lib_path],
                    key=lambda a: a.key):
        r = rows.get(a.key)
        # Restore to its old place (archive paths mirror Library paths).
        want = r["archive"].removeprefix("replaced/") if r and r["archive"] else _lib_path(a)
        a.lib_path = _unique(want, a.key, reserved)
        reserved.add(a.lib_path)


def _lib_path(a: Asset) -> str:
    name = os.path.basename(a.primary.path)
    stem, ext = os.path.splitext(name)
    stem = DUP_RE.sub(r"\g<base>", stem)
    if a.primary.sniffed_ext:
        stem, ext = name, a.primary.sniffed_ext
    if a.taken is None:
        return f"undated/{stem}{ext}"
    t = dt.datetime.fromtimestamp(a.taken).astimezone()  # local time of this host
    return f"{t:%Y/%m}/{t:%Y%m%d_%H%M%S}_{stem}{ext}"


def _unique(path: str, key: str, taken: set[str]) -> str:
    if path not in taken:
        return path
    stem, ext = os.path.splitext(path)
    return f"{stem}_{key.split(':')[1]}{ext}"


# --- 6. Apply ---------------------------------------------------------------

class Refused(SystemExit):
    pass


def check_guards(snap: Snapshot, d: Diff, state: library.State, allow_mass: bool) -> list[str]:
    notes = []
    active = len(state.by_status("active"))
    if d.archived and not snap.complete:
        notes.append(f"snapshot incomplete: NOT archiving {len(d.archived):,} missing items")
        d.archived = []
    if len(d.archived) > max(active * MAX_ARCHIVE_FRACTION, MAX_ARCHIVE_MIN) and not allow_mass:
        raise Refused(f"{len(d.archived):,} of {active:,} items would be archived (> "
                      f"{MAX_ARCHIVE_FRACTION:.0%}). Check the download; re-run with "
                      f"--allow-mass-archive if this is really intended.")
    return notes


def execute(snap: Snapshot, assets: dict[str, Asset], d: Diff, state: library.State,
            base: Path, takeout: Path, workers: int, limit: int | None) -> dict:
    lib, arc = base / "Library", base / "Archive"
    run, stamp = snap.export, library.now()
    stats = collections.Counter()

    def new_row(a: Asset, xmp_hash: str | None) -> dict:
        return {"key": a.key, "home": a.lib_path, "archive": None,
                "item_ids": library.dumps(a.item_ids), "taken": a.taken, "date_how": a.taken_how,
                "xmp_hash": xmp_hash, "user_xmp": 0, "status": "active", "note": None,
                "first_seen": run, "last_seen": run}

    # Gone from Google Photos -> Archive/ (kept forever).
    for row in d.archived:
        dest = library.unique_path(arc / row["home"])
        library.move_with_sidecar(lib / row["home"], dest)
        row.update(status="archived", home=None, archive=str(dest.relative_to(arc)),
                   note=f"gone from Google Photos ({run})")
        state.save(row)
        stats["archived"] += 1
    state.commit()

    # Replaced: archive the old version; the new one (usually at the same path) is added below.
    carry: dict[str, dict] = {}
    for old, a in d.replaced:
        dest = library.unique_path(arc / "replaced" / old["home"])
        library.move_with_sidecar(lib / old["home"], dest)
        if old["user_xmp"]:
            carry[a.key] = old                    # keep the user's sidecar edits
            if (m := state.manual.get(old["key"])):
                a.taken = m["ts"]
                a.date = dates.Decision(m["ts"], "manual", [], [], [])
                state.set_manual(a.key, m["ts"], m["source"], m["csv_value"])
        old.update(status="replaced", home=None, archive=str(dest.relative_to(arc)),
                   note=f"replaced by {a.key} ({run})")
        state.save(old)
        stats["replaced"] += 1
    state.commit()

    # Back in Google Photos -> out of Archive/.
    for a in d.restored:
        row = state.rows[a.key]
        library.move_with_sidecar(arc / row["archive"], lib / a.lib_path)
        h = row["xmp_hash"]
        if not row["user_xmp"]:
            h = library.write_sidecar(lib / a.lib_path, sidecar_bytes(a), a.taken)
        row.update(status="active", home=a.lib_path, archive=None, taken=a.taken,
                   date_how=a.taken_how, xmp_hash=h, note=f"restored ({run})", last_seen=run)
        state.save(row)
        stats["restored"] += 1
    state.commit()

    # New files.
    adds = sorted(d.added + [a for _, a in d.replaced], key=lambda a: a.lib_path)
    if limit is not None:
        adds = adds[:limit]
    jobs = [library.AddJob(a.key, takeout / a.primary.part, a.primary.member, lib / a.lib_path,
                           a.primary.size, a.primary.crc, a.taken) for a in adds]
    by_key = {a.key: a for a in adds}
    done, failed = library.extract(jobs, workers) if jobs else ([], [])
    for n, j in enumerate(done, 1):
        a = by_key[j.key]
        row = new_row(a, None)
        if (old := carry.get(a.key)) is not None:
            side = xmp.sidecar_path(arc / old["archive"])
            if side.exists():
                shutil.copy2(side, xmp.sidecar_path(j.dest))
            row.update(user_xmp=1, xmp_hash=old["xmp_hash"])
        else:
            row["xmp_hash"] = library.write_sidecar(j.dest, sidecar_bytes(a), a.taken)
        state.save(row)
        stats["added"] += 1
        if n % 500 == 0:
            state.commit()
    state.commit()
    for j, err in failed:
        log.error("failed to add %s from %s: %s", j.dest, j.part.name, err)
    stats["failed"] = len(failed)

    # Date changes: rewrite our sidecar; never touch a user-edited one except for a CSV override.
    for a, row in d.redated:
        media = lib / row["home"]
        if row["user_xmp"]:
            if state.manual.get(a.key, {}).get("source") == "csv":
                library.set_sidecar_date(media, a.taken)
        else:
            row["xmp_hash"] = library.write_sidecar(media, sidecar_bytes(a), a.taken)
        row.update(taken=a.taken, date_how=a.taken_how, last_seen=run)
        state.save(row)
        stats["redated"] += 1

    state.con.executemany("UPDATE assets SET last_seen = ? WHERE key = ?",
                          [(run, a.key) for a in d.unchanged])
    state.con.execute("INSERT INTO runs (export, started, finished, summary) VALUES (?,?,?,?)",
                      (run, stamp, library.now(), json.dumps(stats)))
    state.commit()
    return stats


# --- 7. Report --------------------------------------------------------------

def report(snap: Snapshot, sources: list[Source], skipped: list[str],
           assets: dict[str, Asset], d: Diff, state: library.State, h: library.Harvest | None,
           sample: int) -> list[str]:
    out = []
    p = out.append
    gb = lambda n: f"{n / 1e9:,.1f} GB"
    st = collections.Counter(r["status"] for r in state.rows.values())
    p(f"Export:   {snap.export}")
    p(f"Parts:    {len(snap.parts)} ({len(snap.zips)} zip, {len(snap.loose)} loose), "
      f"numbered {min(snap.parts):03d}-{max(snap.parts):03d}")
    p(f"Files:    {len(sources):,} media in snapshot -> {len(assets):,} unique "
      f"({gb(sum(int(k.split(':')[0]) for k in assets))})")
    how = collections.Counter(s.sidecar_how or "none" for s in sources)
    p(f"Sidecars: " + ", ".join(f"{k}={v:,}" for k, v in how.most_common()))
    th = collections.Counter(a.taken_how for a in assets.values())
    p(f"Dates:    " + ", ".join(f"{k}={v:,}" for k, v in th.most_common()))
    p(f"          (agree = sources consistent; manual = your edit; see dates CSV)")
    p(f"Albums:   {len({al for a in assets.values() for al in a.albums})} album folders")
    p(f"Library:  " + ", ".join(f"{k} {st.get(k, 0):,}" for k in
                                 ("active", "archived", "replaced", "deleted")) + " in state DB")
    if h is not None:
        p(f"Immich:   {len(h.xmp_edits):,} sidecars edited (kept as manual dates), "
          f"{len(h.trashed):,} newly trashed (copied to Archive/), "
          f"{len(h.missing):,} deleted (never re-added), {len(h.csv):,} CSV date overrides")
    p("")
    p("Plan:")
    p(f"  add       {len(d.added):>7,}  ({gb(sum(a.primary.size for a in d.added))})")
    p(f"  replace   {len(d.replaced):>7,}  (old version -> Archive/replaced/)")
    p(f"  archive   {len(d.archived):>7,}  (gone from Google Photos -> Archive/)")
    p(f"  restore   {len(d.restored):>7,}  (back in Google Photos, Archive/ -> Library/)")
    p(f"  re-date   {len(d.redated):>7,}  (sidecar date updated; file not moved)")
    p(f"  skip      {len(d.deleted_skipped):>7,}  (you deleted them in Immich)")
    p(f"  unchanged {len(d.unchanged):>7,}")

    def section(title, items, fmt):
        if items:
            p("")
            p(f"{title} (showing {min(sample, len(items))} of {len(items):,}):")
            for it in items[:sample]:
                p("  " + fmt(it))

    section("Add", sorted(d.added, key=lambda a: a.lib_path),
            lambda a: f"{a.lib_path}   <- {a.primary.path}")
    section("Replace", d.replaced, lambda t: f"{t[0]['home']} <- {t[1].primary.path}")
    section("Archive", d.archived, lambda r: r["home"])
    section("Restore", d.restored, lambda a: a.lib_path)
    section("Re-date", d.redated, lambda t: f"{t[1]['home']}: {dates.fmt(t[1]['taken'])} -> "
                                            f"{dates.fmt(t[0].taken)} ({t[0].taken_how})")
    by_how = lambda h: sorted((a for a in assets.values() if a.taken_how == h),
                              key=lambda a: a.primary.path)
    date_fmt = lambda a: f"{a.primary.path}: -> {dates.fmt(a.taken)}   [{a.date.describe()}]"
    section("Google date replaced", by_how("override") + by_how("google-implausible"), date_fmt)
    section("Year taken from file name", by_how("year-from-name"), date_fmt)
    section("Conflicting dates, kept Google's (review)", by_how("conflict-kept-google"), date_fmt)
    section("Only implausible dates, kept Google's (review)", by_how("implausible-kept"), date_fmt)
    section("Google date looks like an upload time but nothing better (kept)",
            by_how("upload-time-kept"), date_fmt)
    section("Undated (Library/undated/)", by_how("none"), date_fmt)
    section("Skipped non-media files", skipped, str)

    warnings = list(snap.warnings) + (h.warnings if h else [])
    active = len(state.by_status("active"))
    if len(d.archived) > max(active * MAX_ARCHIVE_FRACTION, MAX_ARCHIVE_MIN):
        warnings.append(f"MASS ARCHIVE: {len(d.archived):,} of {active:,} library items would be "
                        f"archived (> {MAX_ARCHIVE_FRACTION:.0%}); --apply will refuse")
    if not snap.complete and d.archived:
        warnings.append("Snapshot looks incomplete: --apply will skip archiving")
    if warnings:
        p("")
        p("Warnings:")
        out.extend(f"  - {w}" for w in warnings)
    return out


def write_dates_csv(assets: dict[str, Asset], path: Path) -> None:
    """Every asset whose date is not simply agreed by its sources, for human review."""
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["decision", "chosen", "google", "embedded", "filename", "takeout_path",
                    "library_path", "evidence"])
        for a in sorted(assets.values(), key=lambda a: (a.taken_how, a.primary.path)):
            if a.taken_how in ("agree", "google-only"):
                continue
            by_src = {c.src: dates.fmt(c.ts) for c in a.date.cands}
            w.writerow([a.taken_how, dates.fmt(a.taken), by_src.get("google", ""),
                        by_src.get("embedded", ""), by_src.get("filename", ""),
                        a.primary.path, a.lib_path, a.date.describe()])


# --- main -------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", type=Path, default=DEFAULT_BASE,
                    help="base directory holding Takeout/, Library/, state/ ... (default %(default)s)")
    ap.add_argument("--takeout", type=Path, help="Takeout download dir (default BASE/Takeout)")
    ap.add_argument("--sample", type=int, default=20, help="example lines per section")
    ap.add_argument("--workers", type=int, default=6, help="parallel workers (default %(default)s)")
    ap.add_argument("--no-probe", action="store_true",
                    help="skip reading embedded dates (Google + filename only; fast)")
    ap.add_argument("--apply", action="store_true", help="perform the changes")
    ap.add_argument("--limit", type=int, help="with --apply: add at most N new files (trial run)")
    ap.add_argument("--delete-input", action="store_true",
                    help="with --apply: delete the Takeout parts after a fully successful run")
    ap.add_argument("--allow-mass-archive", action="store_true",
                    help=f"allow archiving more than {MAX_ARCHIVE_FRACTION * 100:.0f}%% of the library, "
                         "or that many photos newly deleted in Immich")
    ap.add_argument("--immich-url", default="http://localhost:2283")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    base = args.base
    takeout = args.takeout or base / "Takeout"

    lock = None
    if args.apply:
        (base / "state").mkdir(parents=True, exist_ok=True)
        lock = open(base / "state" / "sync.lock", "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit("another takeout_sync --apply is running")
        if n := library.clean_partials(base / "Library"):
            log.info("removed %d partial files from an interrupted run", n)

    snap = discover(takeout)
    log.info("export %s: %d parts", snap.export, len(snap.parts))
    sources, skipped = index(snap)
    log.info("indexed %d media files", len(sources))
    assets = build_assets(sources)

    state = library.State(base / "state" / "library.sqlite", readonly=not args.apply)
    client, lib_id, trashed = immich.Client.from_state(base / "state", args.immich_url), None, None
    if client is not None:
        try:
            lib_id = client.library_id()
            total, offline = client.counts(lib_id)
            if offline > max(total * library.MAX_MISSING_FRACTION, 20):
                raise SystemExit(
                    f"Immich reports {offline:,} of {total:,} photos offline: it can't see "
                    f"{base / 'Library'}. Check the Library mount in immich/"
                    f"docker-compose.override.yml (scripts/immich-as-user.sh rewrites it).")
            trashed = client.trashed_paths(lib_id)
            if args.apply:
                client.ensure_trash_days()
        except immich.ImmichError as e:
            log.warning("Immich unavailable (%s); trashed photos will be checked next run", e)
            client = None
    h = library.harvest(state, base, trashed, args.allow_mass_archive)
    if args.apply:
        library.apply_harvest(state, base, h)
        library.ensure_overrides_template(base)
    manual = {k: m["ts"] for k, m in state.manual.items()}
    if not args.apply:   # dry run: show the effect of edits without recording them
        manual.update({k: ts for k, ts in h.xmp_edits.items() if ts is not None})
        manual.update({k: ts for k, (ts, _) in h.csv.items()})
        for k in h.missing:
            state.rows[k] = dict(state.rows[k], status="deleted", home=None)

    assign_dates(assets, takeout, base, args.workers, not args.no_probe, manual)
    d = diff(assets, state.rows)
    plan_paths(assets, state.rows, d)
    lines = report(snap, sources, skipped, assets, d, state, h, args.sample)

    stats = None
    if args.apply:
        for note in check_guards(snap, d, state, args.allow_mass_archive):
            log.warning(note)
        stats = execute(snap, assets, d, state, base, takeout, args.workers, args.limit)
        lines += ["", "Applied: " + ", ".join(f"{k} {v:,}" for k, v in stats.items())]
        if client is not None and lib_id:
            try:
                client.scan(lib_id)
                log.info("Immich library rescan requested")
            except immich.ImmichError as e:
                log.warning("could not trigger Immich rescan: %s", e)

    text = "\n".join(lines)
    print(("\nAPPLIED\n\n" if args.apply else "\nDRY RUN - nothing was changed\n\n") + text)
    logs = base / "logs"
    if logs.is_dir():
        stamp = f"{dt.datetime.now():%Y%m%d-%H%M%S}"
        f = logs / f"{stamp}-{'apply' if args.apply else 'dryrun'}-{snap.export}.txt"
        f.write_text(text + "\n")
        log.info("report written to %s", f)
        f = logs / f"{stamp}-dates-{snap.export}.csv"
        write_dates_csv(assets, f)
        log.info("date decisions needing review written to %s", f)

    if args.apply and args.delete_input:
        if stats["failed"] or args.limit is not None:
            log.warning("not deleting Takeout input: %s",
                        "some files failed" if stats["failed"] else "--limit run")
        else:
            for part in snap.zips + snap.loose:
                part.unlink()
            log.info("deleted %d Takeout parts", len(snap.zips) + len(snap.loose))
    return 1 if stats and stats["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
