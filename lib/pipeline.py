#!/usr/bin/env python3
"""photo-archive: monthly Google Photos backup pipeline.

  1. Download   new files from the Drive Takeout folder (service account, read-only rclone)
  2. Verify     each file against Drive's md5, every zip opens, no missing export parts
  3. Tag        verified files in Drive; the Apps Script (running as you) trashes them
  4. Library    takeout_sync --apply --delete-input: update Library/, Archive/, Immich

Usage:
  photo-archive run [--force]   what cron calls; exits at once if this month already succeeded
  photo-archive --dry-run       show what a run would do, change nothing
  photo-archive status          recent outcomes, warnings, anything needing attention

Every run ends in one outcome, appended to state/pipeline/history.log:
  OK           new files downloaded and/or the library updated
  NOTHING_NEW  no new export in Drive and nothing waiting locally
  TRANSIENT    network, export still arriving, checksum mismatch... the next run retries
  NEEDS_HUMAN  credentials, folder sharing, a library-update failure or safety guard, or
               repeated transient failures. Also writes NEEDS_ATTENTION and logs to syslog.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import drive  # noqa: E402
import takeout_sync  # noqa: E402

CONFIG_FILE = Path(os.environ.get("PHOTO_ARCHIVE_CONFIG",
                                  Path.home() / ".config" / "photo-archive" / "config.env"))
DEFAULTS = {
    "BASE_DIR": "", "RCLONE_BIN": str(Path.home() / ".local/bin/rclone"),
    "RCLONE_REMOTE": "photo-archive-drive", "SERVICE_ACCOUNT_FILE": "", "SERVICE_ACCOUNT_EMAIL": "",
    "FOLDER_ID": "", "FOLDER_NAME": "Takeout", "IMMICH_URL": "http://localhost:2283",
    "WORKERS": "6",
    "SETTLE_HOURS": "2",            # don't touch an export whose files changed this recently
    "STALE_TAG_DAYS": "7",          # verified files still in Drive after this: Apps Script broken?
    "EXPORT_OVERDUE_DAYS": "75",    # > one export interval (monthly or 2-monthly), for a year at a time
    "TRANSIENT_ESCALATE_AFTER": "3",
}

log = logging.getLogger("photo-archive")


class Outcome(Exception):
    def __init__(self, kind: str, msg: str):
        super().__init__(msg)
        self.kind = kind


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if not CONFIG_FILE.exists():
        raise Outcome("NEEDS_HUMAN", f"config file missing: {CONFIG_FILE} (run install.sh)")
    for line in CONFIG_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            cfg[k.strip()] = " ".join(shlex.split(v)) if v.strip() else ""
    for k in ("BASE_DIR", "SERVICE_ACCOUNT_FILE", "FOLDER_ID"):
        if not cfg[k]:
            raise Outcome("NEEDS_HUMAN", f"{k} is not set in {CONFIG_FILE} (run install.sh)")
    return cfg


class Pipeline:
    def __init__(self, cfg: dict, dry_run: bool):
        self.cfg = cfg
        self.dry_run = dry_run
        self.base = Path(cfg["BASE_DIR"])
        self.staging = self.base / "Takeout"
        self.state = self.base / "state" / "pipeline"
        (self.state / "logs").mkdir(parents=True, exist_ok=True)
        self.run_id = dt.datetime.now().strftime("%Y-%m-%dT%H%M%S")
        self.run_log = self.state / "logs" / f"{self.run_id}.log"
        self.warnings: list[str] = []
        self.summary: dict[str, int | str] = {}

    # --- state files ---
    def _read(self, name: str, default: str = "") -> str:
        p = self.state / name
        return p.read_text().strip() if p.exists() else default

    def _write(self, name: str, value: str) -> None:
        (self.state / name).write_text(value + "\n")

    # --- the run ---
    def run(self) -> None:
        client = drive.Client(Path(self.cfg["SERVICE_ACCOUNT_FILE"]), self.cfg["FOLDER_ID"])
        files = self._drive(client)
        candidates = [f for f in files if not f.verified]
        self._check_staleness(files)

        if candidates:
            newest = max(f.modified for f in candidates)
            settle = dt.timedelta(hours=float(self.cfg["SETTLE_HOURS"]))
            if dt.datetime.now(dt.timezone.utc) - newest < settle:
                raise Outcome("TRANSIENT", f"export still arriving in Drive (newest file "
                                           f"{newest:%Y-%m-%d %H:%M} UTC); will retry")
        local = self._local_photo_exports()
        log.info("Drive: %d file(s), %d new; local: %d Photos export(s) waiting",
                 len(files), len(candidates), len(local))
        if not candidates and not local:
            raise Outcome("NOTHING_NEW", f"no new files in Drive '{self.cfg['FOLDER_NAME']}'")
        if self.dry_run:
            print(f"dry run: would download {len(candidates)} file(s) "
                  f"({sum(f.size for f in candidates) / 1e9:.1f} GB): "
                  + ", ".join(f.name for f in candidates[:5]) + (" ..." if len(candidates) > 5 else ""))
            print(f"dry run: Photos exports waiting locally: {local or 'none'}")
            return

        if candidates:
            self._write("last_export_seen", max(f.modified for f in candidates).isoformat())
            self._download(candidates)
            verified = self._verify(candidates)
            self._tag(client, verified)
        self._update_library()
        self._write("last_success", dt.date.today().strftime("%Y-%m"))
        raise Outcome("OK", " ".join(f"{k}={v}" for k, v in self.summary.items()))

    def _drive(self, client: drive.Client) -> list[drive.DriveFile]:
        try:
            folder = client.folder()
            if not folder.get("capabilities", {}).get("canEdit"):
                raise Outcome("NEEDS_HUMAN", f"the service account can only view '{folder['name']}'; "
                                             f"share it with {self.cfg['SERVICE_ACCOUNT_EMAIL']} as "
                                             f"Editor so verified files can be tagged")
            return client.list_files()
        except drive.DriveError as e:
            if e.kind == "auth":
                raise Outcome("NEEDS_HUMAN", f"{e} -- fix it and re-run install.sh")
            if e.kind == "gone":
                raise Outcome("NEEDS_HUMAN", f"Drive folder '{self.cfg['FOLDER_NAME']}' not found: "
                                             f"deleted, or no longer shared with "
                                             f"{self.cfg['SERVICE_ACCOUNT_EMAIL']}? ({e})")
            raise Outcome("TRANSIENT", str(e))

    def _check_staleness(self, files: list[drive.DriveFile]) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        stale_days = float(self.cfg["STALE_TAG_DAYS"])
        stale = [f for f in files if f.verified and now - f.tag[0] > dt.timedelta(days=stale_days)]
        if stale:
            self.warnings.append(
                f"{len(stale)} verified file(s) still in Drive after {stale_days:g} days: is the "
                f"Apps Script trigger running? (https://script.google.com, Triggers)")
        seen = self._read("last_export_seen")
        overdue = float(self.cfg["EXPORT_OVERDUE_DAYS"])
        if seen and now - dt.datetime.fromisoformat(seen) > dt.timedelta(days=overdue):
            self.warnings.append(
                f"no new Takeout export since {seen[:10]}: Takeout's scheduled exports stop after "
                f"a year -- set up a new one at https://takeout.google.com (deliver to Drive, every "
                f"month, Google Photos only)")

    def _local_photo_exports(self) -> list[str]:
        if not self.staging.is_dir():
            return []
        return list(takeout_sync.photo_exports(self.staging)[0])

    # --- 1. download ---
    def _download(self, files: list[drive.DriveFile]) -> None:
        self.staging.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(self.staging).free
        need = sum(f.size for f in files if not (self.staging / f.name).exists())
        if need > free * 0.9:
            raise Outcome("NEEDS_HUMAN", f"not enough disk space: need {need / 1e9:.0f} GB, "
                                         f"{free / 1e9:.0f} GB free on {self.staging}")
        lst = self.state / "logs" / f"{self.run_id}.files"
        lst.write_text("".join(f.name + "\n" for f in files))
        log.info("downloading %d file(s), %.1f GB", len(files), need / 1e9)
        cmd = [self.cfg["RCLONE_BIN"], "copy", f"{self.cfg['RCLONE_REMOTE']}:", str(self.staging),
               f"--drive-root-folder-id={self.cfg['FOLDER_ID']}", "--files-from", str(lst),
               "--retries", "3", "--low-level-retries", "10", "--log-level", "INFO",
               "--log-file", str(self.run_log), "--stats", "0"]
        rc = subprocess.run(cmd, stdin=subprocess.DEVNULL).returncode
        if rc != 0:
            text = self.run_log.read_text(errors="replace")
            if any(s in text for s in ("invalid_grant", "invalid_client", "accessNotConfigured",
                                       "didn't find section in config", "couldn't find section")):
                raise Outcome("NEEDS_HUMAN", f"rclone can't sign in (exit {rc}); re-run install.sh")
            raise Outcome("TRANSIENT", f"download failed (rclone exit {rc}); see {self.run_log}")
        self.summary["downloaded"] = len(files)

    # --- 2. verify ---
    def _verify(self, files: list[drive.DriveFile]) -> list[drive.DriveFile]:
        cache_file = self.state / "verified.json"   # name -> [size, mtime, md5]
        cache = json.loads(cache_file.read_text()) if cache_file.exists() else {}
        good, bad = [], []
        for f in files:
            p = self.staging / f.name
            st = p.stat() if p.exists() else None
            if st is None or st.st_size != f.size:
                bad.append(f"{f.name}: {'missing' if st is None else 'size differs'}")
                p.unlink(missing_ok=True)   # re-download on the next run
                continue
            c = cache.get(f.name)
            if c and c[0] == st.st_size and c[1] == st.st_mtime and c[2] == f.md5:
                md5 = c[2]
            else:
                log.info("checksumming %s", f.name)
                md5 = _md5(p)
                cache[f.name] = [st.st_size, st.st_mtime, md5]
            if md5 == f.md5:
                good.append(f)
            else:
                bad.append(f"{f.name}: md5 differs")
                p.unlink()                  # re-download on the next run
        cache = {k: v for k, v in cache.items() if (self.staging / k).exists()}
        cache_file.write_text(json.dumps(cache))
        if bad:
            raise Outcome("TRANSIENT", f"{len(bad)} file(s) failed verification and will be "
                                       f"re-downloaded: {'; '.join(bad[:3])}")
        problems = takeout_sync.check(self.staging)
        if problems:
            raise Outcome("TRANSIENT", "export incomplete or damaged (Takeout may still be "
                                       "delivering it): " + "; ".join(problems[:3]))
        self.summary["verified"] = len(good)
        return good

    # --- 3. tag in Drive, for the Apps Script to trash ---
    def _tag(self, client: drive.Client, files: list[drive.DriveFile]) -> None:
        try:
            for f in files:
                client.tag_verified(f)
        except drive.DriveError as e:
            # Not fatal: the files are safe locally; tagging is retried next run.
            self.warnings.append(f"could not tag verified files in Drive ({e}); they stay in "
                                 f"Drive until a later run tags them")
            return
        self.summary["tagged"] = len(files)

    # --- 4. library ---
    def _update_library(self) -> None:
        for _ in range(6):
            exports = self._local_photo_exports()
            if not exports:
                break
            log.info("updating library from %s", exports[0])
            cmd = [sys.executable, str(HERE / "takeout_sync.py"), "--base", str(self.base),
                   "--takeout", str(self.staging), "--apply", "--delete-input",
                   "--workers", self.cfg["WORKERS"], "--immich-url", self.cfg["IMMICH_URL"],
                   "--sample", "10"]
            with open(self.run_log, "a") as out:
                rc = subprocess.run(cmd, stdout=out, stderr=subprocess.STDOUT).returncode
            if rc != 0:
                tail = self.run_log.read_text(errors="replace").strip().splitlines()[-1:]
                raise Outcome("NEEDS_HUMAN", f"library update failed (exit {rc}): "
                                             f"{tail[0] if tail else ''} -- see {self.run_log}")
            self.summary["exports"] = int(self.summary.get("exports", 0)) + 1
        else:
            raise Outcome("NEEDS_HUMAN", "Photos exports keep reappearing in the staging folder")
        for p in takeout_sync.index_only_exports(self.staging):
            p.unlink()                      # archive_browser.html-only zips: nothing to keep

    # --- outcome bookkeeping ---
    def finish(self, kind: str, msg: str) -> int:
        stamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        warn = f" [{len(self.warnings)} warning(s)]" if self.warnings else ""
        with open(self.state / "history.log", "a") as h:
            h.write(f"{stamp} {kind:<11} {msg}{warn}\n")
        log.info("OUTCOME %s: %s", kind, msg)
        for w in self.warnings:
            log.warning(w)
            _syslog("warning", w)
        if self.warnings:
            self._write("WARNINGS", "\n".join(self.warnings))
        else:
            (self.state / "WARNINGS").unlink(missing_ok=True)
        fails = self.state / "consecutive_transient_failures"
        if kind in ("OK", "NOTHING_NEW"):
            fails.unlink(missing_ok=True)
            (self.state / "NEEDS_ATTENTION").unlink(missing_ok=True)
            print(f"{kind}: {msg}")
            return 0
        if kind == "TRANSIENT":
            n = int(self._read("consecutive_transient_failures", "0")) + 1
            fails.write_text(f"{n}\n")
            if n < int(self.cfg.get("TRANSIENT_ESCALATE_AFTER", 3)):
                print(f"photo-archive: transient failure ({n} in a row), will retry: {msg}",
                      file=sys.stderr)
                return 75
            kind, msg = "NEEDS_HUMAN", f"{n} transient failures in a row; latest: {msg}"
            with open(self.state / "history.log", "a") as h:
                h.write(f"{stamp} {kind:<11} {msg}\n")
        self._write("NEEDS_ATTENTION", f"{stamp}\n{msg}\nSee {self.run_log}")
        print(f"photo-archive: NEEDS A HUMAN: {msg} (log: {self.run_log})", file=sys.stderr)
        _syslog("err", f"NEEDS_HUMAN: {msg}")
        return 1


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        while chunk := f.read(16 << 20):
            h.update(chunk)
    return h.hexdigest()


def _syslog(level: str, msg: str) -> None:
    if shutil.which("logger"):
        subprocess.run(["logger", "-p", f"user.{level}", "-t", "photo-archive", msg], check=False)


def status(cfg: dict) -> int:
    st = Path(cfg["BASE_DIR"]) / "state" / "pipeline"
    for name, title in (("NEEDS_ATTENTION", "*** NEEDS ATTENTION ***"), ("WARNINGS", "Warnings:")):
        if (st / name).exists():
            print(title)
            print((st / name).read_text())
    for name, title in (("last_success", "Last successful month"),
                        ("last_export_seen", "Newest export seen in Drive")):
        if (st / name).exists():
            print(f"{title}: {(st / name).read_text().strip()}")
    hist = st / "history.log"
    print("\n".join(hist.read_text().splitlines()[-10:]) if hist.exists() else "No runs yet.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("command", nargs="?", default="run", choices=["run", "status"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="run even if this month already succeeded")
    args = ap.parse_args(argv)
    try:
        cfg = load_config()
    except Outcome as o:
        print(f"photo-archive: {o}", file=sys.stderr)
        return 1
    if args.command == "status":
        return status(cfg)

    p = Pipeline(cfg, args.dry_run)
    lock = open(p.state / "lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("photo-archive: another run is in progress, skipping", file=sys.stderr)
        return 0
    handlers = [logging.FileHandler(p.run_log)]
    if sys.stdout.isatty():
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    this_month = dt.date.today().strftime("%Y-%m")
    if not (args.force or args.dry_run) and p._read("last_success") == this_month:
        log.info("already succeeded this month (%s); nothing to do", this_month)
        return 0
    try:
        p.run()
        return 0                         # dry run
    except Outcome as o:
        if args.dry_run:
            print(f"dry run: {o.kind}: {o}")
            return 0
        return p.finish(o.kind, str(o))
    except Exception as e:               # a bug: never die silently under cron
        log.exception("unexpected error")
        return p.finish("NEEDS_HUMAN", f"unexpected {type(e).__name__}: {e}")
    finally:
        for f in sorted((p.state / "logs").glob("*"))[:-200]:
            f.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
