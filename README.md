# photo-archive

Keeps a browsable, self-hosted copy of your Google Photos on your own Linux machine. It updates
itself every month (or every two) without your help, and never loses a photo.

```
Google Takeout ──monthly or────▶ Google Drive /Takeout
               every 2 months
                                        │  1. download (read-only service account)
                                        ▼
                         your machine:  2. verify (checksums, every zip, no missing parts)
                                        3. mark verified ──▶ Apps Script in your Google
                                        │                     account trashes them in Drive
                                        ▼
                                        4. dedupe, date, file under Library/YYYY/MM/
                                        ▼
                                   Immich  http://<this-machine>:2283
```

- **Unattended**: a cron job matched to your Takeout schedule, with automatic retries and a clear status when something needs you.
- **Dates you can trust**: Google's dates are often wrong (upload times, re-encodes, scans). Each photo's
  date is cross-checked against EXIF, file names, album titles and neighbouring camera frames.
- **Full or incremental exports**: Google sends only new and edited photos after the first export;
  photo-archive handles both kinds automatically.
- **Nothing is ever deleted**: photos removed from Google Photos (seen when a full export arrives), and old versions of changed photos, move
  to `Archive/`. Photos you delete in Immich are kept in `Archive/deleted-in-immich/`.
- **Your edits stick**: dates and captions you fix in Immich are never overwritten by later exports.
- **Drive is cleaned up**: each export is removed from Drive once it's safely downloaded, so it doesn't
  sit there using your Google storage.
- **Small**: Python 3 standard library plus exiftool, rclone and Docker (for [Immich](https://immich.app)).

## Setup

Part of the setup happens in your Google account, where no script can reach. **Follow
[docs/setup.md](docs/setup.md)**, which gives click-by-click instructions for every step. In short:

1. **Google Takeout**: schedule a Google Photos export to Drive, monthly (recommended) or every 2 months.
   A one-off export works too, run by hand.
2. **Google Cloud**: create a service account with the Drive API enabled, and download its JSON key.
3. **Google Drive**: share the Takeout folder with the service account as Editor.
4. **Your machine**: `git clone https://github.com/chagar01/photo-archive.git && photo-archive/install.sh`.
   It sets up Docker, Immich, rclone, the key, the folder, and a schedule matching your Takeout frequency.
5. **Apps Script**: paste the generated script into script.google.com and run `install` once.
6. **Every year**: renew the Takeout schedule (photo-archive reminds you when exports stop).

Requirements: Debian or Ubuntu (x86-64 or ARM64), disk space of about 2.2× your photo library, and Google
storage with room for one export.

## Update

```sh
cd photo-archive && git pull && ./install.sh --update
```

This applies the new version with your saved settings and asks no questions. Your photos, Immich's data
and your settings are kept, and Immich's own version is left alone. If the Apps Script changed, it tells
you to paste the new one. To change a setting instead (data folder, Takeout frequency, schedule…), run
`./install.sh` without `--update`.

## Use

```sh
photo-archive status          # last runs, warnings, anything that needs you
photo-archive --dry-run       # what a run would do now
photo-archive run --force     # run now, even if this month already succeeded (or for a one-off export)
```

- **Fix a date**: edit it in Immich; it sticks. For many photos, add `path,date` lines to
  `<data>/state/date_overrides.csv`.
- **Review dates**: every date photo-archive had to work out is listed with its evidence in
  `<data>/logs/*-dates-*.csv`.
- A run that needs you writes `<data>/state/pipeline/NEEDS_ATTENTION` and logs to syslog. See
  [Troubleshooting](docs/setup.md#troubleshooting).

## Safety

- Drive files are trashed only after their checksums match locally, and only by a script in *your*
  account that touches files carrying photo-archive's "verified" mark. Drive's trash keeps them 30 days.
- Local export zips are deleted only after the library update succeeds with no failed files.
- A run refuses to continue if an export has missing parts, if more than 5% of the library would be
  archived or deleted at once, or if Immich or the disk seems unable to see the Library.
- Library paths never change, so Immich keeps your faces, albums and edits.

## Limitations

- Google Photos albums aren't recreated in Immich (the photos are there, filed by date).
- Linux with apt only. Takeout must be delivered as .zip to Google Drive (not Dropbox, OneDrive, or email).
- Tested with Immich v3.3.3.

## Development

```sh
python3 tests/test_takeout_sync.py && python3 tests/test_pipeline.py
```

[CLAUDE.md](CLAUDE.md) holds the design notes and the reasons behind each decision. `scripts/make-dist.sh`
builds a tarball for installing on a machine without git.

## License

[MIT](LICENSE)
