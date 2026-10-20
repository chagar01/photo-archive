"""Pipeline tests with a fake Drive (a local folder + in-memory descriptions) and a fake
rclone, driving the real takeout_sync library update. Run: python3 tests/test_pipeline.py"""
import datetime as dt
import hashlib
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

LIB = Path(__file__).resolve().parent.parent / "lib"
sys.path.insert(0, str(LIB))
root = Path(tempfile.mkdtemp(prefix="photo_archive_test_"))
os.environ["PHOTO_ARCHIVE_CONFIG"] = str(root / "config.env")
import drive  # noqa: E402
import pipeline  # noqa: E402

FAKE_DRIVE = root / "drive"; FAKE_DRIVE.mkdir()
BASE = root / "base"
DESCRIPTIONS: dict[str, str] = {}
CAN_EDIT = [True]

# rclone stand-in: "copy remote: DEST --files-from LIST ..." copies from FAKE_DRIVE.
# A file listed in CORRUPT gets damaged bytes, to exercise verification.
rclone = root / "rclone"
rclone.write_text(f"""#!/usr/bin/env python3
import sys, shutil, pathlib
a = sys.argv[1:]; dest = pathlib.Path(a[2]); names = pathlib.Path(a[a.index('--files-from') + 1]).read_text().split()
corrupt = pathlib.Path('{root}/CORRUPT'); bad = corrupt.read_text().split() if corrupt.exists() else []
for n in names:
    shutil.copy(pathlib.Path('{FAKE_DRIVE}') / n, dest / n)
    if n in bad: (dest / n).write_bytes(b'garbage')
""")
rclone.chmod(0o755)
(root / "config.env").write_text(f"""BASE_DIR='{BASE}'
RCLONE_BIN='{rclone}'
SERVICE_ACCOUNT_FILE='{root}/key.json'
SERVICE_ACCOUNT_EMAIL='sa@x.iam.gserviceaccount.com'
FOLDER_ID='folder123'
IMMICH_URL='http://127.0.0.1:9'
WORKERS='1'
""")


class FakeClient:
    def __init__(self, key_file, folder_id):
        pass

    def folder(self):
        return {"name": "Takeout", "capabilities": {"canEdit": CAN_EDIT[0]}}

    def list_files(self):
        out = []
        for p in sorted(FAKE_DRIVE.iterdir()):
            out.append(drive.DriveFile(p.name, p.name, p.stat().st_size,
                                       hashlib.md5(p.read_bytes()).hexdigest(),
                                       dt.datetime.fromtimestamp(p.stat().st_mtime, dt.timezone.utc),
                                       DESCRIPTIONS.get(p.name, "")))
        return out

    def tag_verified(self, f):
        DESCRIPTIONS[f.name] = drive.tag_text(f)


drive.Client = FakeClient


def export(stamp, files, parts=(1,), age_hours=24):
    """Write a Takeout export into the fake Drive: photo zip(s) + the archive_browser zip."""
    paths = []
    for n in parts:
        p = FAKE_DRIVE / f"takeout-{stamp}-1-{n:03d}.zip"
        paths.append(p)
        with zipfile.ZipFile(p, "w") as z:
            if n != parts[0]:
                z.writestr("Takeout/Google Photos/x/metadata.json", "{}")
                continue
            for path, (data, url, t) in files.items():
                z.writestr("Takeout/Google Photos/" + path, data)
                z.writestr(f"Takeout/Google Photos/{path}.supplemental-metadata.json",
                           json.dumps({"url": url, "photoTakenTime": {"timestamp": str(t)}}))
    idx = FAKE_DRIVE / f"takeout-{stamp}-001.zip"
    with zipfile.ZipFile(idx, "w") as z:
        z.writestr("Takeout/archive_browser.html", "<html/>")
    paths.append(idx)
    old = (dt.datetime.now() - dt.timedelta(hours=age_hours)).timestamp()
    for p in paths:
        os.utime(p, (old, old))
    return paths


def run(*args):
    return pipeline.main(["run", *args])


def last_outcome():
    return (BASE / "state/pipeline/history.log").read_text().splitlines()[-1].split()[1]


J = lambda tag: b"\xff\xd8\xff" + tag
PHOTOS = {"Photos from 2020/a.jpg": (J(b"a"), "u1", 1600000000),
          "Photos from 2021/b.jpg": (J(b"b"), "u2", 1610000000)}

# 1. A complete export: downloaded, verified, tagged, library updated, staging emptied.
export("20261103T010000Z", PHOTOS)
assert run() == 0 and last_outcome() == "OK", (BASE / "state/pipeline/history.log").read_text()
assert all(drive.TAG_RE.match(DESCRIPTIONS[n]) for n in os.listdir(FAKE_DRIVE))
assert len(list((BASE / "Library").rglob("*.jpg"))) == 2
assert not list((BASE / "Takeout").glob("*.zip")), list((BASE / "Takeout").iterdir())
assert (BASE / "state/pipeline/last_success").read_text().strip() == dt.date.today().strftime("%Y-%m")

# 2. Same month again: exits at once without a new history line.
n = len((BASE / "state/pipeline/history.log").read_text().splitlines())
assert run() == 0 and len((BASE / "state/pipeline/history.log").read_text().splitlines()) == n

# 3. Forced, Apps Script hasn't trashed the tagged files yet: they are not re-downloaded.
assert run("--force") == 0 and last_outcome() == "NOTHING_NEW"

# 4. Apps Script trashed them; a new export is still arriving (fresh mtimes): retry later.
for name in list(os.listdir(FAKE_DRIVE)):
    (FAKE_DRIVE / name).unlink()
PHOTOS2 = dict(PHOTOS, **{"Photos from 2022/c.jpg": (J(b"c"), "u3", 1650000000)})
new = export("20261203T010000Z", PHOTOS2, age_hours=0)
assert run("--force") == 75 and last_outcome() == "TRANSIENT"

# 5. Settled, but one file arrives damaged: verification fails, the bad copy is removed.
for p in new:
    os.utime(p, (p.stat().st_mtime - 86400,) * 2)
(root / "CORRUPT").write_text(new[0].name)
assert run("--force") == 75 and "verification" in (BASE / "state/pipeline/history.log").read_text()
assert not (BASE / "Takeout" / new[0].name).exists()
assert not DESCRIPTIONS.get(new[0].name)            # nothing tagged while unverified

# 6. Third transient in a row escalates to NEEDS_HUMAN.
assert run("--force") == 1 and last_outcome() == "NEEDS_HUMAN"
assert (BASE / "state/pipeline/NEEDS_ATTENTION").exists()

# 7. Clean download now succeeds, clears the attention flag, adds the new photo.
(root / "CORRUPT").unlink()
assert run("--force") == 0 and last_outcome() == "OK"
assert not (BASE / "state/pipeline/NEEDS_ATTENTION").exists()
assert len(list((BASE / "Library").rglob("*.jpg"))) == 3

# 8. A verified file still in Drive after a week: Apps Script warning, run still OK.
name = new[0].name
week_ago = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=8)
f = next(x for x in FakeClient(0, 0).list_files() if x.name == name)
DESCRIPTIONS[name] = drive.tag_text(f, week_ago)
assert run("--force") == 0
assert "Apps Script" in (BASE / "state/pipeline/WARNINGS").read_text()

# 9. An export with a missing part is not trusted (nor tagged).
for n in list(os.listdir(FAKE_DRIVE)):
    (FAKE_DRIVE / n).unlink()
gap = export("20270103T010000Z", PHOTOS2, parts=(1, 3))
assert run("--force") == 75 and "missing part" in (BASE / "state/pipeline/history.log").read_text()
assert not any(DESCRIPTIONS.get(p.name) for p in gap)

# 10. Folder shared as Viewer only: needs a human.
CAN_EDIT[0] = False
assert run("--force") == 1 and "Editor" in (BASE / "state/pipeline/NEEDS_ATTENTION").read_text()
# 11. The service-account JWT is a valid RS256 signature over header.claims.
import base64, subprocess, importlib
importlib.reload(drive)                         # the real Client, not the fake
subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-out", str(root / "k.pem")],
               check=True, capture_output=True)
subprocess.run(["openssl", "pkey", "-in", str(root / "k.pem"), "-pubout", "-out", str(root / "k.pub")],
               check=True, capture_output=True)
(root / "key.json").write_text(json.dumps({"client_email": "sa@x.iam.gserviceaccount.com",
                                           "private_key": (root / "k.pem").read_text()}))
jwt = drive.Client(root / "key.json", "f")._jwt()
head_claims, sig = jwt.rsplit(".", 1)
(root / "sig").write_bytes(base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4)))
ok = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(root / "k.pub"), "-signature",
                     str(root / "sig")], input=head_claims.encode(), capture_output=True)
assert ok.returncode == 0, ok.stdout + ok.stderr
claims = json.loads(base64.urlsafe_b64decode(head_claims.split(".")[1] + "=="))
assert claims["scope"] == drive.SCOPE and claims["aud"] == drive.TOKEN_URL
print("all pipeline tests passed")
