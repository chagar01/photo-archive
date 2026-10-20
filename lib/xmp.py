"""Minimal XMP sidecars (<media>.xmp) carrying the date, GPS and description we
decided on. Immich reads these in preference to the file's own metadata, and
writes user edits (date, description, location) back into the same file.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
from pathlib import Path
from xml.sax.saxutils import escape

_TEMPLATE = """<?xpacket begin='﻿' id='W5M0MpCehiHzreSzNTczkc9d'?>
<x:xmpmeta xmlns:x='adobe:ns:meta/' x:xmptk='takeout_sync'>
 <rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>
  <rdf:Description rdf:about=''
    xmlns:exif='http://ns.adobe.com/exif/1.0/'
    xmlns:xmp='http://ns.adobe.com/xap/1.0/'
    xmlns:photoshop='http://ns.adobe.com/photoshop/1.0/'
    xmlns:dc='http://purl.org/dc/elements/1.1/'>
{body}  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end='w'?>
"""


def sidecar_path(media: Path) -> Path:
    return media.with_name(media.name + ".xmp")


def build(ts: int | None, offset_min: int | None, lat: float | None, lon: float | None,
          description: str = "") -> bytes:
    lines = []
    if ts is not None:
        tz = (dt.timezone(dt.timedelta(minutes=offset_min)) if offset_min is not None
              else dt.datetime.fromtimestamp(ts).astimezone().tzinfo)
        stamp = dt.datetime.fromtimestamp(ts, tz).isoformat(timespec="seconds")
        for tag in ("exif:DateTimeOriginal", "xmp:CreateDate", "photoshop:DateCreated"):
            lines.append(f"   <{tag}>{stamp}</{tag}>")
    if lat is not None and lon is not None:
        lines.append(f"   <exif:GPSLatitude>{_coord(lat, 'NS')}</exif:GPSLatitude>")
        lines.append(f"   <exif:GPSLongitude>{_coord(lon, 'EW')}</exif:GPSLongitude>")
    if description:
        lines.append("   <dc:description><rdf:Alt><rdf:li xml:lang='x-default'>"
                     f"{escape(description)}</rdf:li></rdf:Alt></dc:description>")
    return _TEMPLATE.format(body="".join(l + "\n" for l in lines)).encode()


def _coord(value: float, hemis: str) -> str:
    """XMP GPS format: 'DDD,MM.mmmmmmH'."""
    h = hemis[0] if value >= 0 else hemis[1]
    value = abs(value)
    deg = int(value)
    return f"{deg},{(value - deg) * 60:.6f}{h}"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_digest(path: Path) -> str | None:
    try:
        return digest(path.read_bytes())
    except FileNotFoundError:
        return None


_DATE_RE = re.compile(r"DateTimeOriginal(?:>|=['\"])\s*([0-9]{4}-[0-9]{2}-[0-9]{2}T[^<'\"]+)")


def read_date(path: Path) -> int | None:
    """Capture date from a sidecar (ours or one Immich rewrote), as UTC epoch seconds."""
    try:
        m = _DATE_RE.search(path.read_text(errors="replace"))
    except FileNotFoundError:
        return None
    if not m:
        return None
    raw = m[1].strip().replace("Z", "+00:00")
    try:
        t = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.astimezone()
    return int(t.timestamp())
