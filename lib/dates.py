"""Capture-date heuristics for Google Takeout media.

Google's photoTakenTime is often wrong (bulk uploads, scans, edited dates,
camera clock resets), so it is treated as one opinion among several:

  candidates  google    sidecar photoTakenTime (exact UTC instant)
              embedded  EXIF / QuickTime / RIFF / XMP date inside the file
              filename  full date in the name (IMG_20200101_123456, epoch ms ...)
  hints       years mentioned in the filename ("Verano 2009 384.jpg") or in
              album titles ("Les Arcs 2025") -- year only, never used alone.

Implausible candidates are dropped (placeholder camera dates, HEIC before
2017, digital-camera files before 1990, future dates). The rest are grouped
into clusters that agree within CLUSTER_TOLERANCE; each cluster scores one
point per source plus one per hint whose year it matches. The best cluster
wins, ties going to the cluster containing Google's date (so a deliberate
correction made in Google Photos survives when nothing contradicts it).

Embedded metadata is read with exiftool and cached in state/probe_cache.sqlite
keyed by asset content, so each file is probed once ever.
"""
from __future__ import annotations

import collections
import concurrent.futures
import dataclasses
import datetime as dt
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import zipfile
from pathlib import Path

log = logging.getLogger("takeout_sync.dates")

CLUSTER_TOLERANCE = 15 * 3600   # timezone slop between local EXIF and UTC sources
HEAD_BYTES = 256 << 10          # enough for EXIF in JPEG/HEIC/RAW/PNG
VIDEO_TAIL_BYTES = 16 << 20     # MOV/MP4 'moov' atom (with dates) is usually at the end
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".3gp", ".mpg", ".mpeg", ".wmv", ".m4v", ".mkv",
              ".webm", ".mts", ".m2ts"}
EXIF_TAGS = [  # in order of preference for the "embedded" candidate
    "EXIF:DateTimeOriginal", "XMP:DateTimeOriginal", "EXIF:CreateDate",
    "QuickTime:CreationDate",        # Apple Keys, has timezone
    "QuickTime:CreateDate",          # UTC per spec (some cameras write local)
    "QuickTime:MediaCreateDate", "RIFF:DateTimeOriginal", "XMP:DateCreated",
    "XMP:CreateDate", "PNG:CreationTime",
]
EXTRA_TAGS = ["EXIF:OffsetTimeOriginal", "EXIF:OffsetTime", "EXIF:Make", "EXIF:Model",
              "EXIF:Software"]
# Camera clock resets and format epochs; exact local wall-clock matches.
PLACEHOLDERS = {"1904-01-01 00:00:00", "1970-01-01 00:00:00", "1980-01-01 00:00:00",
                "2000-01-01 00:00:00", "2001-01-01 00:00:00", "2002-12-08 12:00:00",
                "2004-01-01 00:00:00", "2005-01-01 00:00:00", "2007-01-01 00:00:00"}
CAMERA_NAME_RE = re.compile(
    r"^(_?DSC[NF_]?|IMG[_-]?\d|PXL_|MVI_|MOV_|VID_|GOPR|GH\d|DJI_|P\d{7}|PANO_|SAM_|CIMG|"
    r"PICT|HPIM|KIMG|Screenshot)", re.I)
CAMERA_MAKES = {"apple", "canon", "nikon", "nikon corporation", "sony", "panasonic", "fujifilm",
                "olympus", "olympus imaging corp.", "samsung", "google", "huawei", "xiaomi",
                "lge", "motorola", "gopro", "dji", "oneplus", "htc", "nokia", "leica", "pentax",
                "ricoh", "casio computer co.,ltd.", "eastman kodak company"}
HEIC_EPOCH = dt.datetime(2017, 6, 1, tzinfo=dt.timezone.utc).timestamp()
DIGITAL_EPOCH = dt.datetime(1990, 1, 1, tzinfo=dt.timezone.utc).timestamp()
FIRST_PHOTO = dt.datetime(1826, 1, 1, tzinfo=dt.timezone.utc).timestamp()

DATE_RE = re.compile(r"^(\d{4}):(\d{2}):(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?"
                     r"(?:\.\d+)?\s*(Z|[+-]\d{2}:?\d{2})?")
FN_DATETIME_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})[-_.]?(0[1-9]|1[0-2])[-_.]?"
                            r"(0[1-9]|[12]\d|3[01])[ _T.-]?(?:at )?"
                            r"([01]\d|2[0-3])[-_.:h]?([0-5]\d)[-_.:m]?([0-5]\d)")
FN_DATE_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})[-_.]?(0[1-9]|1[0-2])[-_.]?"
                        r"(0[1-9]|[12]\d|3[01])(?!\d)")
FN_DMY_RE = re.compile(r"^(0[1-9]|[12]\d|3[01])(0[1-9]|1[0-2])((?:19|20)\d{2})\d{0,4}$")  # 01082009008
FN_EPOCH_MS_RE = re.compile(r"(?<!\d)(1[0-9]{12})(?!\d)")
HEXID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-|[0-9a-f]{16}", re.I)  # UUIDs / hashes
YEAR_RE = re.compile(r"(?<!\d)(18[3-9]\d|19\d\d|20[0-4]\d)(?!\d)")


# --- Probing embedded metadata ----------------------------------------------

CACHE_SCHEMA = "CREATE TABLE IF NOT EXISTS probe (key TEXT PRIMARY KEY, tags TEXT NOT NULL)"


def probe(jobs: list[tuple[str, Path, str | None, str]], cache_db: Path, tmp_root: Path,
          workers: int) -> dict[str, dict]:
    """Return {asset key: exiftool tags}. jobs = [(key, part path, zip member|None, ext)]."""
    cache_db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(cache_db)
    con.execute(CACHE_SCHEMA)
    cached = {k: json.loads(t) for k, t in con.execute("SELECT key, tags FROM probe")}
    todo = [j for j in jobs if j[0] not in cached]
    if not todo:
        con.close()
        return cached
    if not shutil.which("exiftool"):
        log.warning("exiftool not installed; skipping embedded dates for %d files", len(todo))
        con.close()
        return cached

    by_part: dict[Path, list] = collections.defaultdict(list)
    for key, part, member, ext in todo:
        by_part[part].append((key, member, ext))
    log.info("probing embedded dates of %d files in %d parts (%d workers, cached thereafter)",
             len(todo), len(by_part), workers)
    tmp_root.mkdir(parents=True, exist_ok=True)
    done = 0
    with concurrent.futures.ProcessPoolExecutor(workers) as pool:
        futs = [pool.submit(_probe_part, part, items, str(tmp_root)) for part, items in
                sorted(by_part.items(), key=lambda kv: -len(kv[1]))]
        for fut in concurrent.futures.as_completed(futs):
            results = fut.result()
            con.executemany("INSERT OR REPLACE INTO probe VALUES (?, ?)",
                            [(k, json.dumps(t)) for k, t in results.items()])
            con.commit()
            cached.update(results)
            done += 1
            log.info("probed part %d/%d", done, len(futs))
    con.close()
    return cached


def _probe_part(part: Path, items: list[tuple[str, str | None, str]], tmp_root: str,
                batch: int = 64) -> dict[str, dict]:
    out: dict[str, dict] = {}
    tmp = Path(tempfile.mkdtemp(prefix="probe_", dir=tmp_root))
    try:
        if items[0][1] is None:                       # loose file: probe in place
            key, _, _ = items[0]
            out[key] = _exiftool([str(part)]).get(str(part), {})
            return out
        with zipfile.ZipFile(part) as zf:
            infos = {i.filename: i for i in zf.infolist()}
            items = sorted(items, key=lambda it: infos[it[1]].header_offset)  # sequential IO
            for start in range(0, len(items), batch):
                files = {}
                for n, (key, member, ext) in enumerate(items[start:start + batch]):
                    f = tmp / f"{n:03d}{ext or '.bin'}"
                    _write_probe_copy(zf, infos[member], f, ext.lower() in VIDEO_EXTS)
                    files[str(f)] = key
                tags = _exiftool(list(files))
                for f, key in files.items():
                    out[key] = tags.get(f, {})
                    os.unlink(f)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out


def _write_probe_copy(zf: zipfile.ZipFile, info: zipfile.ZipInfo, dest: Path, video: bool):
    """Photos: first HEAD_BYTES. Videos: head + tail written sparsely at the true offsets,
    so atom/chunk offsets stay valid without storing the whole file."""
    with zf.open(info) as src, open(dest, "wb") as out:
        head = src.read(HEAD_BYTES)
        out.write(head)
        if not video or info.file_size <= HEAD_BYTES:
            return
        tail = bytearray()
        while chunk := src.read(8 << 20):
            tail += chunk
            if len(tail) > 2 * VIDEO_TAIL_BYTES:
                del tail[:-VIDEO_TAIL_BYTES]
        tail = tail[-VIDEO_TAIL_BYTES:]
        out.seek(max(len(head), info.file_size - len(tail)))
        out.write(tail[-(info.file_size - len(head)):])


def _exiftool(paths: list[str]) -> dict[str, dict]:
    cmd = ["exiftool", "-j", "-q", "-q", "-G0", "-charset", "filename=utf8",
           *[f"-{t}" for t in EXIF_TAGS + EXTRA_TAGS], *paths]
    r = subprocess.run(cmd, capture_output=True, text=True)
    try:
        rows = json.loads(r.stdout) if r.stdout.strip() else []
    except ValueError:
        log.warning("unparseable exiftool output for %s", paths[:3])
        rows = []
    return {row.pop("SourceFile"): row for row in rows}


# --- Deciding the date ------------------------------------------------------

@dataclasses.dataclass
class Cand:
    src: str        # google | embedded | filename | sequence
    ts: float       # UTC epoch seconds
    detail: str     # human-readable origin, e.g. "EXIF:DateTimeOriginal 2009:07:14 18:02:11"
    exact_tz: bool  # True when the instant is known (not local time guessed in host tz)
    wall: dt.datetime | None = None   # embedded: naive local wall-clock time as recorded
    offset_min: int | None = None     # embedded: explicit UTC offset, when recorded


@dataclasses.dataclass
class Decision:
    ts: int | None
    how: str        # agree | google-only | embedded-only | filename-only | override |
                    # google-implausible | conflict-kept-google | year-from-name |
                    # implausible-kept | upload-time-kept | none
    cands: list[Cand]
    rejected: list[tuple[Cand, str]]
    hints: list[tuple[int, str]]
    offset_min: int | None = None   # UTC offset where taken, if known; else host timezone

    def describe(self) -> str:
        bad = {id(c) for c, _ in self.rejected}
        parts = [f"{c.src}={fmt(c.ts)}" for c in self.cands if id(c) not in bad]
        parts += [f"{c.src}={fmt(c.ts)} (rejected: {why})" for c, why in self.rejected]
        parts += [f"hint {y} ({where})" for y, where in self.hints]
        return "; ".join(parts)


def fmt(ts: float | None) -> str:
    return "-" if ts is None else dt.datetime.fromtimestamp(ts).astimezone().strftime("%Y-%m-%d %H:%M")


@dataclasses.dataclass
class Item:
    key: str
    name: str                  # original file name
    ext: str                   # real extension (sniffed if the name has none)
    google: int | None
    tags: dict
    albums: list[str]


def decide_all(items: list[Item]) -> dict[str, Decision]:
    """Two passes: decide each file alone, then use cross-file evidence (batch
    re-encode dates, camera-sequence neighbours) for the files still in doubt."""
    batch_days = _batch_days(items)
    bulk = _bulk_minutes(items)
    first = {it.key: decide(it, batch_days=batch_days, bulk=bulk) for it in items}
    seq = _SequenceIndex(items, first)
    out = {}
    for it in items:
        d = first[it.key]
        if d.how in WEAK and (c := seq.candidate(it.name)) is not None:
            d = decide(it, batch_days=batch_days, bulk=bulk, extra=[c])
        out[it.key] = d
    return out


# Decisions that cross-file evidence may improve.
WEAK = {"none", "implausible-kept", "upload-time-kept", "conflict-kept-google", "year-from-name"}


def decide(it: Item, batch_days: frozenset[str] = frozenset(),
           bulk: dict[int, int] | None = None, extra: list[Cand] = ()) -> Decision:
    cands: list[Cand] = []
    if it.google is not None:
        cands.append(Cand("google", it.google, "sidecar photoTakenTime", True))
    if (c := _embedded(it.tags)) is not None:
        cands.append(c)
    stem = os.path.splitext(it.name)[0]
    camera_named = bool(CAMERA_NAME_RE.match(it.name))
    if (c := _from_filename(stem)) is not None:
        cands.append(c)
    cands += extra

    this_year = dt.date.today().year
    hints = []
    if not camera_named and not HEXID_RE.search(stem) and not any(c.src == "filename" for c in cands):
        hints += [(int(y), "filename") for y in set(YEAR_RE.findall(stem)) if int(y) <= this_year]
    for album in it.albums:
        hints += [(int(y), f"album '{album}'") for y in set(YEAR_RE.findall(album))
                  if int(y) <= this_year]

    # A device-generated date in the name (IMG_20200101_..., 23072011206) also means
    # the file was born digital.
    digital = camera_named or str(it.tags.get("EXIF:Make", "")).strip().lower() in CAMERA_MAKES \
        or any(c.src == "filename" and c.ts >= DIGITAL_EPOCH for c in cands)
    valid, rejected = [], []
    now = dt.datetime.now(dt.timezone.utc).timestamp() + 2 * 86400
    for c in cands:
        why = None
        if not FIRST_PHOTO <= c.ts <= now:
            why = "out of range"
        elif c.src == "embedded" and c.detail.split(" ", 1)[1] in PLACEHOLDERS:
            why = "camera placeholder date"
        elif c.src == "embedded" and _local_day(c.ts) in batch_days and not any(
                o.src != "embedded" and abs(o.ts - c.ts) <= CLUSTER_TOLERANCE for o in cands):
            why = "batch re-encode date shared by many files"
        elif c.src == "google" and bulk and (n := bulk.get(int(c.ts) // 60)) and not any(
                o.src != "google" and abs(o.ts - c.ts) <= CLUSTER_TOLERANCE for o in cands) \
                and _local_year(c.ts) not in {y for y, _ in hints}:
            why = f"bulk upload time shared by {n} files"
        elif it.ext.lower() in (".heic", ".heif") and c.ts < HEIC_EPOCH:
            why = "HEIC did not exist before 2017"
        elif digital and c.ts < DIGITAL_EPOCH:
            why = "digital camera file dated before 1990"
        (rejected.append((c, why)) if why else valid.append(c))

    name_years = {y for y, where in hints if where == "filename"}
    if not valid:
        # Nothing plausible: keep Google's (or any) date rather than losing the file in
        # undated/ -- it may be a deliberate date on a photo of an old print. Flagged.
        last = next((c for c, _ in rejected if c.src == "google"), rejected[0][0] if rejected else None)
        if last is None:
            return Decision(None, "none", cands, rejected, hints)
        if len(name_years) == 1 and _local_year(last.ts) not in name_years:
            return Decision(_with_year(last.ts, next(iter(name_years))), "year-from-name",
                            cands, rejected, hints)
        how = "upload-time-kept" if any("bulk" in why for _, why in rejected) else "implausible-kept"
        return Decision(int(last.ts), how, cands, rejected, hints)

    clusters = _cluster(valid)
    def score(cl):
        years = {_local_year(c.ts) for c in cl}
        support = len({c.src for c in cl}) + sum(1 for y, _ in hints if y in years)
        has = {c.src for c in cl}
        return (support, "google" in has, "embedded" in has, "filename" in has)
    best = max(clusters, key=score)
    chosen = _most_precise(best)
    srcs = {c.src for c in best}

    # A year a human typed into the file name beats a lone uncorroborated date
    # ("Verano 2009 384.jpg" dated 1946 by Google, no EXIF). Keep month/day/time
    # so the set stays in order.
    if (len(name_years) == 1 and srcs <= {"google"} and _local_year(chosen.ts) not in name_years):
        ts = _with_year(chosen.ts, next(iter(name_years)))
        return Decision(ts, "year-from-name", cands, rejected, hints)

    google_valid = any(c.src == "google" for c in valid)
    google_rejected = any(c.src == "google" for c, _ in rejected)
    if len(clusters) == 1:
        how = "agree" if len(srcs) > 1 else f"{next(iter(srcs))}-only"
        if google_rejected:
            how = "google-implausible"
    elif "google" not in srcs:
        how = "override" if google_valid else "google-implausible"
    elif score(best)[0] == max(score(cl)[0] for cl in clusters if cl is not best):
        how = "conflict-kept-google"   # tie broken in Google's favour: worth a human look
    else:
        how = "agree"                  # Google's cluster won outright over an outlier
    return Decision(int(chosen.ts), how, cands, rejected, hints, _offset(best))


def _offset(cluster: list[Cand]) -> int | None:
    """Local UTC offset where the photo was taken: an explicit EXIF offset, or the
    difference between the camera's wall clock and Google's exact UTC instant."""
    for c in cluster:
        if c.offset_min is not None:
            return c.offset_min
    wall = next((c.wall for c in cluster if c.wall is not None and not c.exact_tz), None)
    google = next((c.ts for c in cluster if c.src == "google"), None)
    if wall is None or google is None:
        return None
    off = (wall.replace(tzinfo=dt.timezone.utc).timestamp() - google) / 60
    off = round(off / 15) * 15
    return int(off) if abs(off) <= 14 * 60 else None


def _cluster(cands: list[Cand]) -> list[list[Cand]]:
    clusters: list[list[Cand]] = []
    for c in sorted(cands, key=lambda c: c.ts):
        if clusters and c.ts - clusters[-1][-1].ts <= CLUSTER_TOLERANCE:
            clusters[-1].append(c)
        else:
            clusters.append([c])
    return clusters


def _most_precise(cluster: list[Cand]) -> Cand:
    order = {"google": 0, "embedded": 1, "filename": 2, "sequence": 3}
    return min(cluster, key=lambda c: (not c.exact_tz, order[c.src]))


def _local_year(ts: float) -> int:
    return dt.datetime.fromtimestamp(ts).astimezone().year


def _local_day(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).astimezone().strftime("%Y-%m-%d")


def _with_year(ts: float, year: int) -> int:
    t = dt.datetime.fromtimestamp(ts)   # naive local wall clock; re-localised below (DST)
    try:
        t = t.replace(year=year)
    except ValueError:                 # 29 Feb
        t = t.replace(year=year, day=28)
    return int(t.timestamp())


BATCH_MIN_FILES = 10
BATCH_MIN_SPREAD = 30 * 86400


def _batch_days(items: list[Item]) -> frozenset[str]:
    """Days on which many files got an embedded date that contradicts their Google
    date, while those Google dates are spread over months: a re-encode/import
    batch (e.g. videos converted 2016-04-12), not a capture date."""
    by_day: dict[str, list[int]] = collections.defaultdict(list)
    for it in items:
        c = _embedded(it.tags)
        if c and it.google is not None and abs(c.ts - it.google) > CLUSTER_TOLERANCE:
            by_day[_local_day(c.ts)].append(it.google)
    days = frozenset(d for d, g in by_day.items()
                     if len(g) >= BATCH_MIN_FILES and max(g) - min(g) >= BATCH_MIN_SPREAD)
    for d in sorted(days):
        log.info("embedded date %s looks like a batch re-encode (%d files); ignoring it", d,
                 len(by_day[d]))
    return days


BULK_MIN_FILES = 10
BULK_WINDOW_MIN = 2             # uploads spread over a few minutes


def _bulk_minutes(items: list[Item]) -> dict[int, int]:
    """Minutes (epoch // 60) at which >= BULK_MIN_FILES files got a Google date that
    nothing else corroborates: Google fell back to the upload time for a batch."""
    counts: collections.Counter[int] = collections.Counter()
    for it in items:
        if it.google is None:
            continue
        others = [c for c in (_embedded(it.tags), _from_filename(os.path.splitext(it.name)[0]))
                  if c is not None]
        if not any(abs(c.ts - it.google) <= CLUSTER_TOLERANCE for c in others):
            counts[it.google // 60] += 1
    window = {m: sum(counts.get(m + d, 0) for d in range(-BULK_WINDOW_MIN, BULK_WINDOW_MIN + 1))
              for m in counts}
    bulk = {m: n for m, n in window.items() if n >= BULK_MIN_FILES}
    log.info("%d files have a Google date that looks like a bulk upload time",
             sum(counts[m] for m in bulk))
    return bulk


SEQ_RE = re.compile(r"^(DSCN|DSCF|DSC_|_DSC|DSC|P\d{3}|IMG_|IMG|MVI_|SAM_|CIMG|PICT|GOPR)(\d{4})",
                    re.I)
SEQ_MAX_GAP = 50                # how far (in frame numbers) to look for neighbours
SEQ_MAX_SPAN = 45 * 86400       # neighbours either side must agree with each other


class _SequenceIndex:
    """Camera frame numbers are monotonic in time: P1020311 sits between P1020289
    and P1020450. Neighbours with trusted (multi-source) dates bracket a doubtful file."""

    def __init__(self, items: list[Item], decisions: dict[str, Decision]):
        self.by_prefix: dict[str, list[tuple[int, int]]] = collections.defaultdict(list)
        for it in items:
            d = decisions[it.key]
            if d.how == "agree" and (m := SEQ_RE.match(it.name)):
                self.by_prefix[m[1].upper()].append((int(m[2]), d.ts))
        for v in self.by_prefix.values():
            v.sort()

    def candidate(self, name: str) -> Cand | None:
        m = SEQ_RE.match(name)
        if not m or not (seq := self.by_prefix.get(m[1].upper())):
            return None
        n = int(m[2])
        lo = [(k, t) for k, t in seq if n - SEQ_MAX_GAP <= k < n]
        hi = [(k, t) for k, t in seq if n < k <= n + SEQ_MAX_GAP]
        if not lo or not hi:
            return None                # need brackets on both sides to trust it
        (k1, t1), (k2, t2) = lo[-1], hi[0]
        if not 0 <= t2 - t1 <= SEQ_MAX_SPAN:
            return None                # frame counter reset or a different camera
        ts = t1 + (t2 - t1) * (n - k1) / (k2 - k1)
        return Cand("sequence", ts, f"between {m[1]}{k1:04d} and {m[1]}{k2:04d}", False)


def _embedded(tags: dict) -> Cand | None:
    for tag in EXIF_TAGS:
        raw = tags.get(tag)
        if not isinstance(raw, str) or raw.startswith("0000"):
            continue
        m = DATE_RE.match(raw.strip())
        if not m:
            continue
        y, mo, d, hh, mi, ss, tz = m.groups()
        try:
            naive = dt.datetime(int(y), int(mo), int(d), int(hh or 12), int(mi or 0), int(ss or 0))
        except ValueError:
            continue
        if tz is None and tag == "EXIF:DateTimeOriginal":
            tz = tags.get("EXIF:OffsetTimeOriginal") or tags.get("EXIF:OffsetTime")
        if tz is None and tag in ("QuickTime:CreateDate", "QuickTime:MediaCreateDate"):
            tz = "Z"                                  # QuickTime spec: UTC
        if tz:
            ts = naive.replace(tzinfo=_tzinfo(tz)).timestamp()
        else:
            ts = naive.astimezone().timestamp()       # local wall clock, host timezone
        detail = f"{tag} {naive:%Y-%m-%d %H:%M:%S}"
        offset = None
        if tz and tz not in ("Z", "z"):
            offset = int(_tzinfo(tz).utcoffset(None).total_seconds() // 60)
        return Cand("embedded", ts, detail, bool(tz), wall=naive, offset_min=offset)
    return None


def _tzinfo(tz: str) -> dt.timezone:
    if tz in ("Z", "z"):
        return dt.timezone.utc
    tz = tz.replace(":", "")
    sign = -1 if tz[0] == "-" else 1
    return dt.timezone(sign * dt.timedelta(hours=int(tz[1:3]), minutes=int(tz[3:5])))


def _from_filename(stem: str) -> Cand | None:
    if m := FN_EPOCH_MS_RE.search(stem):
        return Cand("filename", int(m[1]) / 1000, f"epoch ms {m[1]}", True)
    if m := FN_DATETIME_RE.search(stem):
        try:
            t = dt.datetime(*map(int, m.groups()))
            return Cand("filename", t.astimezone().timestamp(), f"name {m[0]}", False)
        except ValueError:
            pass
    if m := FN_DMY_RE.match(stem):
        try:
            t = dt.datetime(int(m[3]), int(m[2]), int(m[1]), 12)
            return Cand("filename", t.astimezone().timestamp(), f"name {m[0]} (DDMMYYYY)", False)
        except ValueError:
            pass
    if m := FN_DATE_RE.search(stem):
        try:
            t = dt.datetime(*map(int, m.groups()), 12)   # midday: date-only precision
            return Cand("filename", t.astimezone().timestamp(), f"name {m[0]}", False)
        except ValueError:
            pass
    return None
