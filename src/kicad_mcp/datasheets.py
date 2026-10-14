"""Download datasheets into the project so they travel with it."""

from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Several distributor and manufacturer sites refuse obvious bots, so look like a browser.
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36",
    "Accept": "application/pdf,*/*;q=0.8",
}
MAX_BYTES = 80 * 1024 * 1024


class DatasheetError(RuntimeError):
    pass


def safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._+-]+", "_", name).strip("._") or "datasheet"


def download(url: str, dest_dir: Path, name: str, _hops: int = 0) -> Path:
    """Download ``url`` to ``dest_dir/<name>.pdf`` and return the path.

    Raises DatasheetError when the server does not return a PDF (for example a
    cookie wall or a product page), so the caller can ask the user to save it by hand.
    """
    if url.startswith("//"):
        url = "https:" + url
    if not url.lower().startswith(("http://", "https://")):
        raise DatasheetError(f"not a web address: {url!r}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / f"{safe_name(name)}.pdf"
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read(MAX_BYTES + 1)
            final_url = r.geturl()
    except urllib.error.HTTPError as e:
        raise DatasheetError(f"HTTP {e.code} downloading {url}") from e
    except urllib.error.URLError as e:
        raise DatasheetError(f"could not reach {urllib.parse.urlsplit(url).netloc}: {e.reason}") from e
    if len(data) > MAX_BYTES:
        raise DatasheetError("datasheet is larger than 80 MB")
    if not data.startswith(b"%PDF"):
        # some links land on an HTML page that embeds or links the real PDF
        m = re.search(rb'(?:href|src)="([^"]+\.pdf[^"]*)"', data[:500_000], re.IGNORECASE)
        if m and _hops < 2:
            nested = urllib.parse.urljoin(final_url, m.group(1).decode(errors="ignore"))
            if nested != url:
                return download(nested, dest_dir, name, _hops + 1)
        raise DatasheetError(
            f"{urllib.parse.urlsplit(final_url).netloc} did not return a PDF (it may need a browser). "
            f"Open {url} and save the PDF as {target}"
        )
    target.write_bytes(data)
    return target
