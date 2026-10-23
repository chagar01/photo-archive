"""Tiny Immich API client (stdlib only) for the bits takeout_sync needs."""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger("takeout_sync.immich")

CONTAINER_ROOT = "/mnt/library"   # where install.sh mounts Library/ inside Immich
MIN_TRASH_DAYS = 60               # must comfortably exceed the monthly sync interval


class ImmichError(Exception):
    pass


class Client:
    def __init__(self, url: str, key: str):
        self.url = url.rstrip("/") + "/api"
        self.key = key

    @classmethod
    def from_state(cls, state_dir: Path, url: str) -> "Client | None":
        key_file = state_dir / "immich_api_key"
        if not key_file.exists():
            log.warning("no %s; skipping Immich integration", key_file)
            return None
        return cls(url, key_file.read_text().strip())

    def _call(self, method: str, path: str, body=None):
        req = urllib.request.Request(
            self.url + path, method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"x-api-key": self.key, "content-type": "application/json",
                     "accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
        except (urllib.error.URLError, OSError) as e:
            raise ImmichError(f"{method} {path}: {e}") from e
        return json.loads(data) if data else None

    def library_id(self) -> str:
        for lib in self._call("GET", "/libraries"):
            if CONTAINER_ROOT in lib.get("importPaths", []):
                return lib["id"]
        raise ImmichError(f"no external library with import path {CONTAINER_ROOT}")

    def scan(self, library_id: str) -> None:
        self._call("POST", f"/libraries/{library_id}/scan")

    def trashed_paths(self, library_id: str) -> list[str]:
        """Library-relative paths of assets currently in Immich's trash."""
        out, page = [], 1
        while page:
            res = self._call("POST", "/search/metadata", {
                "libraryId": library_id, "withDeleted": True,
                "trashedAfter": "1970-01-01T00:00:00.000Z", "size": 1000, "page": page})
            assets = res["assets"]
            for a in assets["items"]:
                p = a.get("originalPath", "")
                # Offline = Immich can't see the file (Library unmounted or mis-mounted);
                # Immich trashes those itself, so they say nothing about the user's wishes.
                if a.get("isTrashed") and not a.get("isOffline") and p.startswith(CONTAINER_ROOT + "/"):
                    out.append(p[len(CONTAINER_ROOT) + 1:])
            page = int(assets["nextPage"]) if assets.get("nextPage") else 0
        return out

    def counts(self, library_id: str) -> tuple[int, int]:
        """(all assets, offline assets) in the library, trashed ones included."""
        n = lambda **f: self._call("POST", "/search/statistics",
                                   {"libraryId": library_id, "withDeleted": True, **f})["total"]
        return n(), n(isOffline=True)

    def ensure_trash_days(self, days: int = MIN_TRASH_DAYS) -> None:
        cfg = self._call("GET", "/system-config")
        trash = cfg.get("trash", {})
        if trash.get("enabled") and trash.get("days", 0) >= days:
            return
        cfg["trash"] = {"enabled": True, "days": max(days, trash.get("days", 0))}
        self._call("PUT", "/system-config", cfg)
        log.info("Immich trash retention set to %d days (sync copies trashed photos to Archive/)",
                 cfg["trash"]["days"])
