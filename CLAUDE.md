# photo-archive

Monthly, unattended Google Photos backup into a self-hosted, browsable archive (Immich).
Supersedes `~/code/google_drive_mirror` (download only) and `~/code/google_photo_mirror`
(Takeout browser automation; broken since 2026-08 by Google's login checks).

## Flow (`lib/pipeline.py`, cron: `0 1 3-9 * *`)
1. **Download**: `rclone copy` from the Drive Takeout folder into `BASE/Takeout/` (staging). This uses a service account
   with a read-only rclone remote, and copies only files that aren't tagged yet.
2. **Verify**: each file's size and md5 must match Drive's (cached in `state/pipeline/verified.json`), every zip must open, and
   there must be no gaps in an export's part numbers (`takeout_sync.check`). The step doesn't start while a file changed under 2 h ago (export still arriving).
3. **Tag**: the service account sets each verified file's Drive *description* to
   `photo-archive verified <time> md5=… size=…`. `apps_script/trash_verified.gs` runs daily **as the owner** and
   trashes only tagged files with an unchanged size. A service account can't trash files it doesn't own; that's
   why the Apps Script exists. The service account needs **Editor** on the folder (metadata only), plus the `drive` API scope.
4. **Library**: `takeout_sync.py --apply --delete-input`, looping over the exports in staging, oldest first. Index-only
   zips (holding just `archive_browser.html`) are deleted afterwards.

Outcomes (`state/pipeline/history.log`): OK / NOTHING_NEW / TRANSIENT (exit 75, escalates after 3) / NEEDS_HUMAN
(the `NEEDS_ATTENTION` file plus syslog). `last_success` (YYYY-MM) makes the 4th–9th cron runs no-ops once the month has succeeded.
Warnings (in `WARNINGS`): tagged files still in Drive after 7 days, meaning the Apps Script isn't running. No new export for 75 days
means the Takeout schedule (which runs every 2 months, for a year) has expired.

## Layout
- `lib/`: everything at runtime. `pipeline.py` (orchestrator), `drive.py` (Drive API as the service account; the JWT is
  signed with the `openssl` CLI, so it's stdlib only), `takeout_sync.py` (discover, index, diff, plan, apply, report), `dates.py`,
  `library.py` (state DB, harvesting Immich edits, file operations), `xmp.py`, `immich.py`.
- `install.sh`: interactive and idempotent. It installs apt packages and Docker, sets up Immich (API key and external library via the API),
  rclone, the service-account key, the folder (checked for Editor), the Apps Script instructions, config and cron.
  It installs code into `~/.local/lib/photo-archive`, `~/.local/bin/photo-archive` and `~/.config/photo-archive/config.env`.
- `scripts/make-dist.sh [--key KEY.json]`: builds a tarball for another machine.
- `scripts/immich-as-user.sh DATA [--no-start]`: the Immich override (`user:`, mounts) and ownership fixes; install.sh calls it.
- `tests/`: `test_takeout_sync.py` (end-to-end `--apply` over synthetic months, plus the date rules) and `test_pipeline.py`
  (fake Drive and fake rclone through every outcome, plus a real RS256 JWT check). **Run both after any change**:
  `python3 tests/test_takeout_sync.py && python3 tests/test_pipeline.py`. After changing `lib/`, re-run
  `install.sh` (or copy `lib/*.py` to `~/.local/lib/photo-archive/`) so cron uses the new code.

## Library requirements and decisions
- Each Takeout is a full snapshot. Compared with the library, an item is new (add), changed (replace; the old version goes to
  `Archive/replaced/`), or missing (archived to `Archive/`, never hard-deleted). Runs are idempotent, and an interrupted run is safe
  (`.partial` files plus a state commit after each file).
- **Viewer: Immich**, using an external library at `/mnt/library` (= `BASE/Library`), mounted **read-write**.
- **The user fixes dates in Immich.** Edits go to the `.xmp`, are harvested as manual dates, and that sidecar is never rewritten.
  Bulk fixes go in `state/date_overrides.csv` (`path,date`).
- **Deleted in Immich means gone for good**: the photo is copied to `Archive/deleted-in-immich/` while it's in the trash, and never re-added.
- **A Library path never changes** (`YYYY/MM/YYYYMMDD_HHMMSS_name`, from the date decided at first sight). Re-dating rewrites
  only the `.xmp` and the mtime, because moving a file loses its Immich identity (edits, faces, albums). A replacement takes over the old path.
- Guards: no archiving from an incomplete snapshot. A run refuses if more than 5% (and more than 20 items) would be archived, or if
  more than 5% of Library files are missing (e.g. an unmounted disk).
- Python 3, stdlib only. External tools: exiftool (embedded dates), openssl (JWT), rclone, Docker.

## Takeout input quirks
- Parts are `takeout-<ts>-1-NNN.zip` (2 GB each), plus a separate `takeout-<ts>-001.zip` holding only `archive_browser.html`.
  Content is under `Takeout/Google Photos/`: `Photos from YYYY/` plus album folders. **The same photo appears in several
  folders**, so dedupe by content (`"<size>:<crc32>"` from the zip directory, so no extraction is needed to diff).
- Sidecars are `<name>.<ext>.supplemental-metadata[(n)].json`. Matching order: exact → `(n)` duplicate → `-edited` → same stem with
  a still-image extension (Live Photo MOV/MP4, RAW+JPEG). The item id is the sidecar `url` plus the variant, which is how
  "replaced" is detected.
- **Loose media can replace a zip part** (`DVD_114126_0-023.mpg`): the `-NNN` is the part number. The original folder is recovered
  from where the file's sidecar lives.
- Files without an extension (Live Photo clips) are identified by their magic bytes, and the extension is appended in the Library.

## Dates (`lib/dates.py`): don't trust Google's date blindly
- Candidates: Google's `photoTakenTime` (negative = before 1970, which is valid), embedded EXIF/QuickTime/RIFF/XMP (exiftool; cached
  in `state/probe_cache.sqlite`; videos keep their head plus the last 16 MB as a sparse file, since the moov atom is at the end), filename
  dates (`IMG_20200101_123456`, epoch ms, `DDMMYYYYnnn`), and camera-sequence neighbours (`P1020311` sits between trusted
  `P1020289` and `P1020330`, if both are within 50 frames and 45 days). Hints: years in the filename or album titles (not frame numbers or UUIDs).
- Rejected as implausible: placeholder dates; HEIC before 2017; born-digital files dated before 1990; **Google bulk upload times** (≥10
  uncorroborated files within ±2 min); **embedded batch re-encode dates** (≥10 files on one day contradicting Google
  dates spread over ≥30 days).
- Candidates within 15 h of each other form a cluster, scored at 1 point per source plus 1 per matching hint year. Ties go to Google
  (`conflict-kept-google`). A lone uncorroborated date with a single filename year moves to that year (`year-from-name`). With no plausible
  date, Google's is kept but flagged. The local UTC offset comes from EXIF, or from the EXIF wall clock minus Google's UTC time.
  Non-trivial decisions go to `logs/*-dates-*.csv`.

## Immich behaviour (verified on v3.3.3, 2026-10-20)
- Immich reads `<file>.xmp`, and **sidecar values beat EXIF**.
- With an `:ro` mount, UI edits are **silently reverted** within a second (the sidecar write fails, so Immich re-reads the old sidecar).
- With a read-write mount, **permanently deleting a photo deletes the original file and its `.xmp`**. Trash retention is set to 60 days
  (`immich.ensure_trash_days`).
- The API has no `lockedProperties`, so edits are detected by sidecar hash.
- `immich-server` and `immich-machine-learning` run as the installing user (`user:` in the override; ML caches bind-mounted
  under `immich/ml-cache/`), so Immich-written `.xmp` files stay editable by the pipeline. Postgres/valkey keep their image users.

## Working rules
- Run the tests after every change. Do a dry run (`takeout_sync.py` without `--apply`, or `photo-archive --dry-run`) before
  real runs on real data.
- Never delete or overwrite in `Library/` or `Archive/` without logging it. Ask before installing system packages or
  starting multi-hour jobs.
