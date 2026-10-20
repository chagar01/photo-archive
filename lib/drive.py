"""Google Drive access as a service account, stdlib only (JWT signed with the
openssl CLI). Used to list the Takeout folder with Drive's checksums and to tag
files we have downloaded and verified, so the Apps Script running as the owner
can trash them (a service account can't trash files it doesn't own).

The tag lives in the file's description, which Apps Script can read with plain
DriveApp:  "photo-archive verified <UTC ISO time> md5=<md5> size=<bytes>"
"""
from __future__ import annotations

import base64
import dataclasses
import datetime as dt
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://www.googleapis.com/drive/v3"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/drive"
FOLDER_MIME = "application/vnd.google-apps.folder"
TAG_RE = re.compile(r"^photo-archive verified (\S+) md5=([0-9a-f]{32}) size=(\d+)")


class DriveError(Exception):
    """kind: auth (key/API problem), gone (folder missing/unshared), transient."""

    def __init__(self, kind: str, msg: str):
        super().__init__(msg)
        self.kind = kind


@dataclasses.dataclass
class DriveFile:
    id: str
    name: str
    size: int
    md5: str
    modified: dt.datetime
    description: str

    @property
    def tag(self) -> tuple[dt.datetime, str, int] | None:
        m = TAG_RE.match(self.description or "")
        if not m:
            return None
        return dt.datetime.fromisoformat(m[1]), m[2], int(m[3])

    @property
    def verified(self) -> bool:
        t = self.tag
        return t is not None and t[1] == self.md5 and t[2] == self.size


def tag_text(f: DriveFile, when: dt.datetime | None = None) -> str:
    when = (when or dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"photo-archive verified {when} md5={f.md5} size={f.size}"


class Client:
    def __init__(self, key_file: Path, folder_id: str):
        self.key = json.loads(Path(key_file).read_text())
        self.folder_id = folder_id
        self._token: tuple[str, float] | None = None

    # --- auth ---
    def _access_token(self) -> str:
        if self._token and self._token[1] > time.time() + 60:
            return self._token[0]
        body = urllib.parse.urlencode({
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": self._jwt()}).encode()
        res = self._http("POST", TOKEN_URL, body, {"content-type": "application/x-www-form-urlencoded"},
                         auth=False)
        self._token = (res["access_token"], time.time() + int(res.get("expires_in", 3600)))
        return self._token[0]

    def _jwt(self) -> str:
        """RS256-signed assertion for Google's token endpoint (signed by the openssl CLI)."""
        now = int(time.time())
        b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=")
        header = b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        claims = b64(json.dumps({"iss": self.key["client_email"], "scope": SCOPE,
                                 "aud": TOKEN_URL, "iat": now, "exp": now + 3600}).encode())
        signing_input = header + b"." + claims
        with tempfile.TemporaryDirectory() as d:
            pem = Path(d) / "k.pem"
            fd = os.open(pem, os.O_WRONLY | os.O_CREAT, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(self.key["private_key"])
            sig = subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(pem)],
                                 input=signing_input, capture_output=True, check=True).stdout
        return (signing_input + b"." + b64(sig)).decode()

    def _http(self, method: str, url: str, body: bytes | None = None, headers: dict | None = None,
              auth: bool = True):
        headers = dict(headers or {})
        if auth:
            headers["authorization"] = f"Bearer {self._access_token()}"
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
        except urllib.error.HTTPError as e:
            text = e.read().decode(errors="replace")
            if e.code in (400, 401) and re.search(r"invalid_grant|invalid_client|unauthorized_client|"
                                                  r"Invalid JWT|invalid_scope", text):
                raise DriveError("auth", f"Google rejected the service account key: {text[:300]}")
            if e.code == 403 and re.search(r"accessNotConfigured|SERVICE_DISABLED|has not been used",
                                           text):
                raise DriveError("auth", "the Google Drive API is disabled in the service "
                                         "account's project")
            if e.code == 404:
                raise DriveError("gone", f"not found: {url}")
            if e.code == 403 and "insufficientFilePermissions" in text:
                raise DriveError("gone", f"permission denied: {text[:300]}")
            raise DriveError("transient", f"HTTP {e.code} from Drive: {text[:300]}")
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise DriveError("transient", f"could not reach Google: {e}")
        return json.loads(data) if data else None

    def _api(self, method: str, path: str, params: dict | None = None, body: dict | None = None):
        params = {"supportsAllDrives": "true", **(params or {})}
        url = f"{API}{path}?{urllib.parse.urlencode(params)}"
        data = None if body is None else json.dumps(body).encode()
        return self._http(method, url, data, {"content-type": "application/json"})

    # --- operations ---
    def folder(self) -> dict:
        """The Takeout folder's metadata; raises DriveError('gone') if unshared/deleted."""
        f = self._api("GET", f"/files/{self.folder_id}",
                      {"fields": "id,name,mimeType,trashed,capabilities(canEdit)"})
        if f.get("mimeType") != FOLDER_MIME or f.get("trashed"):
            raise DriveError("gone", f"'{f.get('name')}' is not a folder, or is in the trash")
        return f

    def list_files(self) -> list[DriveFile]:
        out, token = [], None
        while True:
            params = {"q": f"'{self.folder_id}' in parents and trashed = false",
                      "fields": "nextPageToken,files(id,name,size,md5Checksum,modifiedTime,"
                                "description,mimeType)",
                      "pageSize": "1000", "includeItemsFromAllDrives": "true"}
            if token:
                params["pageToken"] = token
            res = self._api("GET", "/files", params)
            for f in res.get("files", []):
                if f["mimeType"] == FOLDER_MIME or "md5Checksum" not in f:
                    continue  # subfolders / Google Docs aren't part of a Takeout export
                out.append(DriveFile(f["id"], f["name"], int(f.get("size", 0)), f["md5Checksum"],
                                     dt.datetime.fromisoformat(f["modifiedTime"].replace("Z", "+00:00")),
                                     f.get("description", "")))
            token = res.get("nextPageToken")
            if not token:
                return out

    def tag_verified(self, f: DriveFile) -> None:
        self._api("PATCH", f"/files/{f.id}", {"fields": "id"}, {"description": tag_text(f)})


def main(argv: list[str]) -> int:
    """Installer helper: `drive.py check KEY_FILE FOLDER_ID` prints the folder's name and
    whether the service account can edit it; exit 2 auth, 3 not found, 4 transient."""
    import sys
    if len(argv) != 3 or argv[0] != "check":
        print("usage: drive.py check KEY_FILE FOLDER_ID", file=sys.stderr)
        return 64
    try:
        f = Client(Path(argv[1]), argv[2]).folder()
    except DriveError as e:
        print(str(e), file=sys.stderr)
        return {"auth": 2, "gone": 3}.get(e.kind, 4)
    print(json.dumps({"name": f["name"], "canEdit": bool(f.get("capabilities", {}).get("canEdit"))}))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv[1:]))
