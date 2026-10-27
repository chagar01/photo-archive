# Setup guide

`install.sh` automates everything on your machine. It can't reach into your Google account, so a few
things there are yours to do by hand. This guide walks through all of them in the order that works best.
Plan on about an hour of your own time, plus a wait of hours to days for Google's first export.

| Step | Where | What | Time |
|---|---|---|---|
| [1](#1-schedule-google-takeout) | takeout.google.com | Schedule the Google Photos export | 5 min, then hours–days for Google |
| [2](#2-create-a-google-cloud-service-account) | console.cloud.google.com | Create a service account and its key | 15 min |
| [3](#3-share-the-takeout-folder-with-the-service-account) | drive.google.com | Share the Takeout folder with it | 2 min |
| [4](#4-run-the-installer) | your machine | Run `install.sh` | 20 min + downloads |
| [5](#5-install-the-apps-script) | script.google.com | Install the daily clean-up script | 10 min |
| [6](#6-check-that-it-works) | your machine | Check that it works | 5 min |
| [Yearly](#every-year-renew-the-takeout-schedule) | takeout.google.com | Renew the Takeout schedule | 5 min |

## Before you start

- **A Linux machine** (Debian or Ubuntu, x86-64 or ARM64) that stays on, with `sudo` rights. The installer
  adds Docker, Python 3, exiftool, ffmpeg and rclone.
- **Disk space**: about 2.2× the size of your Google Photos library. That covers the library itself
  plus one full export waiting to be processed. Your library size is shown at
  [one.google.com/storage](https://one.google.com/storage) under "Google Photos".
- **Google storage space**: Takeout puts each export *into your Google Drive*, so it counts against your
  Google storage. You need free space at least the size of your photo library. Exports are moved
  to Drive's trash once they're safely downloaded, but trashed files keep counting for 30 days,
  until Drive empties its trash. For a while you may need room for two exports at once (see
  [Google storage](#google-storage-filling-up)).
- **A Google account** with your photos in it. A personal @gmail.com account is simplest. With a
  Google Workspace account, your administrator may block step 2 (see
  [Can't create a key](#cant-create-a-service-account-key)).

---

## 1. Schedule Google Takeout

Google Takeout is Google's export tool. photo-archive doesn't try to drive it automatically:
Google blocks automated logins. Instead you set it up once, and Google can repeat the export for a year.

1. Open [takeout.google.com](https://takeout.google.com), signed in to the account that holds your photos.
2. Under **Select data to include**, click **Deselect all**, then scroll down and tick **Google Photos**.
   Leave its options alone ("All photo albums included", all formats).
3. Click **Next step** at the bottom.
4. Choose:
   - **Transfer to**: *Add to Drive*
   - **Frequency**: one of the three options. Remember which; the installer asks.

     | Takeout option | photo-archive runs | Good for |
     |---|---|---|
     | *Export every month for 1 year* (12 exports) **recommended** | 01:00 on the 3rd of every month | new photos reach your archive within a month |
     | *Export every 2 months for 1 year* (6 exports) | 01:00 on the 3rd of the month after each export | half the downloads, if bandwidth or Google storage is tight |
     | *Export once* (1 export) | not scheduled: you run `photo-archive run --force` when Google's email arrives | trying it out, or an occasional manual refresh |

     If the 3rd's run fails (network down, export still arriving), it retries daily until the 9th.

     A scheduled export is **full** the first time (your whole library), then **incremental**: only
     photos added or edited since the previous export. Cancelling and re-creating the schedule (e.g. to
     change its day) starts again with a full export. photo-archive tells them apart by itself:
     incremental exports add new and edited photos; full exports also move photos you've deleted in
     Google Photos to `Archive/`. So deletions in Google Photos reach the archive only with a full export.
   - **File type**: *.zip* (photo-archive doesn't read .tgz)
   - **File size**: *2 GB* (any size works; 2 GB parts make retries cheap)
5. Click **Create export**.

Google emails you when the export is ready. For a large library that can take a day or more. The files
appear in your Drive in a folder called **Takeout**, as `takeout-<date>-1-001.zip`, `-002.zip` and so on.

> **Why first?** The first export creates the Takeout folder, which step 3 shares. If you want to carry
> on while it runs, create a folder called `Takeout` at the top level of *My Drive* yourself. Takeout
> normally delivers into it. When the first export arrives, check that it landed there. If Takeout
> made a second folder instead, re-run `install.sh` and pick that one.

A scheduled export stops after a year. See [Every year](#every-year-renew-the-takeout-schedule).

## 2. Create a Google Cloud service account

photo-archive signs in to Drive as a **service account**: a robot Google account that belongs to you. It
has no password, its sign-in never expires, and it sees only what you share with it. It's free, and you
don't need a billing account.

### 2a. Create a project
1. Open [console.cloud.google.com](https://console.cloud.google.com) and accept the terms if asked.
2. Click the project picker at the top left (it may say *Select a project*) > **New project**.
3. Name it, e.g. `photo-archive`, and click **Create**. Make sure the new project is selected in the
   picker before you continue.

### 2b. Turn on the Google Drive API
1. Open [the Google Drive API page](https://console.cloud.google.com/apis/library/drive.googleapis.com)
   (or: menu ☰ > **APIs & Services** > **Library**, search "Google Drive API").
2. Click **Enable**.

If you skip this, the installer stops with "enable the Google Drive API".

### 2c. Create the service account
1. Menu ☰ > **IAM & Admin** > **Service Accounts** > **+ Create service account**.
2. Name it `photo-archive`. The ID fills itself in. Click **Create and continue**.
3. The next two steps (*Permissions* and *Principals with access*) are optional: **skip both**, then click
   **Done**. The service account needs no Cloud roles; its only access is the folder you share with it.
4. Note its email address, which looks like `photo-archive@your-project.iam.gserviceaccount.com`.

### 2d. Create a key
1. Click the service account > **Keys** tab > **Add key** > **Create new key** > **JSON** > **Create**.
2. A `.json` file downloads. This file **is** the service account's password:
   - Copy it to the machine you'll install on (e.g. with `scp`), then delete other copies.
   - Never commit it, email it, or put it in a shared folder. The installer stores it at
     `~/.config/photo-archive/service-account.json`, readable only by you.
   - If it leaks: on the same **Keys** tab, delete the key, create a new one, and re-run `install.sh`.

## 3. Share the Takeout folder with the service account

1. In [Google Drive](https://drive.google.com), find the **Takeout** folder in *My Drive*.
2. Right-click it > **Share** > **Share**.
3. Paste the service account's email from step 2c. Set its role to **Editor**. Untick **Notify people**
   (nobody reads that inbox), and click **Share**. If Drive asks whether you're sure about sharing
   outside your organisation, confirm.

**Why Editor?** photo-archive downloads with read-only access. It uses Editor rights only to write a
note into each file's *description* once the file is downloaded and verified. The clean-up script in
step 5 deletes only files carrying that note. Editor doesn't let the service account delete files you
own, but it could add or change files in that one folder. That's another reason to keep the key private.

You can share the folder before or during the installer. It lists the folders shared with the service
account and waits until one appears.

## 4. Run the installer

On the machine that will keep your archive, as your normal user (**not** with `sudo`; the
installer asks for it when needed):

```sh
git clone https://github.com/chagar01/photo-archive.git
photo-archive/install.sh
```

It's interactive and safe to re-run. Each time, it offers your previous answers as defaults. Here is what
it asks, and what it needs from you:

| Installer step | You provide |
|---|---|
| 1. System packages | your `sudo` password |
| 2. Data folder | where the archive lives (default `~/PhotoArchive`; see disk space above) |
| 3. Immich | **in a browser**: open `http://<this-machine>:2283` and create the Immich admin account, then **Account Settings > API Keys > New API Key** (all permissions). Paste the key into the installer. |
| 4. Code and rclone | nothing |
| 5. Service account | the path to the `.json` key from step 2d |
| 6. Takeout folder | pick it from the list (needs step 3) |
| 7. Apps Script | nothing yet. It writes the script for step 5 and can print it for you to copy |
| 8. Schedule | the Takeout frequency you chose in step 1. For *every 2 months*, also the month the first export arrives. Then Enter to accept the matching schedule (see the table in step 1) |

Notes:
- Immich's first start downloads several GB of Docker images. If GitHub's registry rate-limits the
  download, the installer waits and retries.
- The installer adds you to the `docker` group. That takes effect at your next login and isn't needed
  for photo-archive itself.
- To move the installation to another machine: on the old one, `scripts/make-dist.sh` builds a tarball.
  `scripts/make-dist.sh --key KEY.json` also bundles the key, so treat that tarball as a secret.

## 5. Install the Apps Script

Google doesn't let the service account delete *your* files, even with Editor rights. So a tiny script
runs **inside your own Google account**, once a day. It moves to Drive's trash only those Takeout
files that photo-archive has marked as downloaded and verified. Anything unmarked (for example an export
still arriving) is never touched, and trashed files can be recovered from Drive's trash for 30 days.

The installer wrote your copy of the script, with your folder's ID filled in, to
`~/.config/photo-archive/trash_verified.gs`. To show it: `cat ~/.config/photo-archive/trash_verified.gs`.

1. Open [script.google.com](https://script.google.com), signed in to the account that owns the photos.
   Click **New project**.
2. Click *Untitled project* at the top and rename it `photo-archive`.
3. Select everything in the editor (`Code.gs`), delete it, and paste in the whole script. Save with **Ctrl+S**.
4. In the toolbar, choose the function **`install`** from the drop-down next to *Debug*, then click **Run**.
5. Google asks for permission (once):
   - **Review permissions** > choose your account.
   - *"Google hasn't verified this app"*: that's expected, because the app is your own script. Click
     **Advanced** > **Go to photo-archive (unsafe)**.
   - Allow access to your Google Drive, and allow the script to run while you're away. It needs both
     to trash files on a schedule.
6. The **Execution log** at the bottom should end with `daily trigger installed (around 06:00); folder OK`.

To check it later:
- **Triggers** (clock icon on the left) lists `trashVerifiedTakeoutFiles`, daily.
- **Executions** lists each daily run and what it trashed.
- If a run fails, Google emails you. You can also run `trashVerifiedTakeoutFiles` by hand at any time.

If you skip this step, nothing breaks: downloaded files simply stay in Drive. After 7 days,
`photo-archive status` starts warning that the Apps Script doesn't seem to be running.

## 6. Check that it works

```sh
photo-archive --dry-run     # what a run would do now; changes nothing
photo-archive run --force   # do it now instead of waiting for the 3rd
photo-archive status        # last runs, warnings, anything that needs you
```

The first real run downloads the export, verifies it, files every photo under `Library/YYYY/MM/`,
and tells Immich to scan. Then:
- Open `http://<this-machine>:2283`. Your photos appear as Immich indexes them, which takes a while
  for a big library while it generates thumbnails.
- By the next morning, the Apps Script has moved the export's files to Drive's trash.

## Every year: renew the Takeout schedule

Takeout's scheduled exports end after a year. When no new export has arrived for 45 days (monthly) or
75 days (every 2 months), `photo-archive status` shows `no new Takeout export since …`. Repeat
[step 1](#1-schedule-google-takeout) with the same frequency. Nothing else needs to change, as long as the
export lands in the same Takeout folder. To switch frequency, or for every 2 months if the new schedule
starts in a different month, re-run `install.sh` too, so the run months match.

With *Export once* there's nothing to renew: set up another one-off export whenever you want a refresh.

---

## Updating photo-archive

```sh
cd photo-archive && git pull && ./install.sh --update
```

`--update` applies the new version using the answers you gave last time, without asking anything. It keeps
your photos, Immich's data and your settings, and doesn't change Immich's version. If the Apps Script
changed, it says so: paste the new version over the old one at script.google.com, save, and run `install`
once. If a saved setting no longer works (e.g. the key was revoked), it stops and tells you to run the
interactive `./install.sh`. That's also how you change settings such as the Takeout frequency.

Immich updates separately. Read its [release notes](https://github.com/immich-app/immich/releases) first
(some versions need manual steps), then: `cd <data>/immich && sudo docker compose pull && sudo docker compose up -d`.

## Day-to-day

- **Viewing**: Immich at `http://<this-machine>:2283` in a browser, or the Immich mobile app (server URL
  as above) on your home network.
- **Fixing a date**: edit it in Immich (the *i* panel > date). photo-archive keeps your edit and never
  overwrites it. To fix many at once, add `path,date` lines to `<data>/state/date_overrides.csv`.
- **Reviewing dates**: whenever photo-archive had to choose between conflicting dates, it records the
  choice and its evidence in `<data>/logs/*-dates-*.csv`.
- **Deleting**: delete in Immich as usual. photo-archive keeps a copy in `<data>/Archive/deleted-in-immich/`
  and never re-adds the photo. Photos you delete *in Google Photos* move to `<data>/Archive/` at the
  next export.
- **When something needs you**: a run that can't continue writes
  `<data>/state/pipeline/NEEDS_ATTENTION` and logs to syslog. `photo-archive status` shows the reason.
  The next successful run clears it.

## Troubleshooting

| Message | What to do |
|---|---|
| installer: `enable the Google Drive API` | Do [2b](#2b-turn-on-the-google-drive-api) in the *same project* as the service account, wait a minute, re-run. |
| installer: `Google rejected the key` | The key was deleted or the service account disabled. Create a new key ([2d](#2d-create-a-key)) and re-run. |
| installer: `The service account can only *view* that folder` | Change its share to **Editor** ([step 3](#3-share-the-takeout-folder-with-the-service-account)). |
| installer: `Nothing shared with the service account yet` | Shares take a minute to show; press Enter to refresh. Check you shared with the service account's email, not your own. |
| status: `verified file(s) still in Drive after 7 days` | The Apps Script isn't running. Check its **Triggers** and **Executions** ([step 5](#5-install-the-apps-script)). |
| status: `no new Takeout export since …` | The yearly schedule ended. Repeat [step 1](#1-schedule-google-takeout). |
| status: `Drive folder '…' not found` | The Takeout folder was deleted or unshared. Re-share it, or re-run the installer to pick another. |
| status: `export still arriving` / `export incomplete` | Normal while Takeout delivers. Later runs (up to the 9th) retry. |
| status: `not enough disk space` | Free space on the data disk, then `photo-archive run --force`. |
| status: `Immich reports N of M photos offline` | Immich can't see your Library folder. Run `install.sh --update`, which rewrites Immich's Library mount and checks that Immich can write to it. |
| status: `photos newly in Immich's trash` | More than 5% of photos were deleted in Immich since the last run. If you meant to, run `python3 ~/.local/lib/photo-archive/takeout_sync.py --base <data> --takeout <data>/Takeout --apply --allow-mass-archive` once; otherwise restore them from Immich's trash. |
| Takeout email: export failed, or not enough storage | Free up Google storage ([below](#google-storage-filling-up)); the next scheduled export retries. |

### Can't create a service account key
Workspace (business or school) accounts often enforce the policy
`iam.disableServiceAccountKeyCreation`. An administrator can lift it for your project under
**IAM & Admin > Organization Policies**. Or create the project with a personal @gmail.com account
instead: the service account and the photos can belong to different Google accounts, as long as the
Takeout folder is shared with the service account.

### Google storage filling up
Trashed Takeout files count against your storage until Drive's trash empties them (after 30 days).
To get the space back immediately after a successful run, open [Drive's trash](https://drive.google.com/drive/trash)
and permanently delete the `takeout-…` files.

## Uninstall

```sh
crontab -l | grep -v '# photo-archive' | crontab -          # stop the scheduled job
cd <data>/immich && sudo docker compose down                 # stop Immich (add -v to drop its cache volume)
rm -rf ~/.local/lib/photo-archive ~/.local/bin/photo-archive ~/.config/photo-archive
```

Then, in Google:
- At [script.google.com](https://script.google.com), delete the photo-archive project.
- In the Cloud console, delete the service account (or the whole project).
- Unshare the Takeout folder.

Your photos stay in `<data>/Library` and `<data>/Archive` until you delete them yourself.
