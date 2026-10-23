# photo-archive

Keeps a browsable, self-hosted copy of your Google Photos, updated monthly and unattended.

1. **Download**: Google Takeout delivers a Photos export into your Drive's Takeout folder every
   2 months. photo-archive downloads new files with a read-only Google service account.
2. **Verify**: every file is checked against Drive's checksum, every zip must open, and no export
   parts may be missing.
3. **Clean up Drive**: verified files are marked, and a small Apps Script in your Google account
   moves them to the Drive trash (recoverable for 30 days). Nothing else in the folder is touched.
4. **Update the library**: photos are deduplicated, dated (cross-checking Google's date against
   EXIF, file names and camera sequences), filed under `Library/YYYY/MM/`, and shown in
   [Immich](https://immich.app) at `http://<this-machine>:2283`.

Nothing is ever deleted. Photos removed from Google Photos and old versions of changed photos go to
`Archive/`. Dates or captions you fix in Immich are kept, and photos you delete in Immich are
saved to `Archive/deleted-in-immich/` and never come back.

## Install

```sh
scripts/make-dist.sh                    # on this machine: code only
scripts/make-dist.sh --key ~/key.json   # ...or with the service account key bundled (keep it private)

# on the target machine (Debian/Ubuntu, amd64 or arm64):
tar xzf photo-archive-*.tar.gz && photo-archive/install.sh
```

You need:
- A Google Cloud **service account** JSON key, with the **Google Drive API enabled** in its project.
- Your Drive **Takeout folder shared with the service account's email as Editor**. Editor lets it
  mark verified files; it still can't delete anything you own.
- A Takeout schedule: https://takeout.google.com > only Google Photos > "Add to Drive",
  "Export every 2 months for 1 year", .zip, 2 GB. Re-create it yearly.

The installer walks through system packages, the data folder, Immich (admin account, API key),
rclone, the key, the folder, the Apps Script (paste it into script.google.com, run `install` once)
and the schedule (default: 01:00 on the 3rd of each month, with automatic retries until the 9th).
Re-run it at any time to change settings.

To update an existing machine without the full installer, copy a new tarball over and run
`photo-archive/scripts/immich-as-user.sh [DATA_FOLDER]`. It switches Immich to run as you instead
of root, and gives back any data files a `sudo`-run install left owned by root.

## Use

```sh
photo-archive status          # last runs, warnings, anything that needs you
photo-archive --dry-run       # what a run would do now
photo-archive run --force     # run now, even if this month already succeeded
```

`NEEDS_HUMAN` outcomes also write `<data>/state/pipeline/NEEDS_ATTENTION` and log to syslog.

- **Fix a date:** edit it in Immich; it sticks. For many photos, add `path,date` lines to
  `<data>/state/date_overrides.csv`.
- **Review dates:** every date the pipeline had to work out is listed with its evidence in
  `<data>/logs/*-dates-*.csv`.

## Development

```sh
python3 tests/test_takeout_sync.py && python3 tests/test_pipeline.py
```

See CLAUDE.md for the design and the reasons behind it.
