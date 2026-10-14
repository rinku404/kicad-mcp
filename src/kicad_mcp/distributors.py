"""Part search on Mouser and DigiKey through their official APIs.

Credentials come from environment variables (which MCP clients can set in
their server config, ``"env": {...}``) or from the settings file described in
:mod:`kicad_mcp.config`:

- Mouser:  ``MOUSER_API_KEY`` (Search API key from https://www.mouser.com/api-hub/)
- DigiKey: ``DIGIKEY_CLIENT_ID`` and ``DIGIKEY_CLIENT_SECRET`` (an app with the
  Product Information v4 API at https://developer.digikey.com/), plus optional
  ``DIGIKEY_LOCALE_SITE`` (default ``US``), ``DIGIKEY_LOCALE_CURRENCY`` (default
  ``USD``) and ``DIGIKEY_LOCALE_LANGUAGE`` (default ``en``).
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field

from . import config

TIMEOUT = 30
USER_AGENT = "kicad-mcp (+https://github.com/rinku404/kicad-mcp)"


class DistributorError(RuntimeError):
    pass


@dataclass
class PriceBreak:
    quantity: int
    unit_price: float
    currency: str


@dataclass
class Part:
    source: str  # "mouser" or "digikey"
    distributor_pn: str
    mpn: str
    manufacturer: str
    description: str = ""
    datasheet_url: str = ""
    product_url: str = ""
    stock: int | None = None
    lifecycle: str = ""
    package: str = ""
    min_order_qty: int | None = None
    price_breaks: list[PriceBreak] = field(default_factory=list)
    attributes: dict[str, str] = field(default_factory=dict)

    def to_dict(self, attributes: bool = True) -> dict:
        d = asdict(self)
        if not attributes:
            d.pop("attributes")
        return d

    def unit_price(self, quantity: int) -> PriceBreak | None:
        """Price per unit when buying ``quantity`` (the best break at or below it)."""
        best = None
        for pb in sorted(self.price_breaks, key=lambda p: p.quantity):
            if pb.quantity <= max(quantity, 1):
                best = pb
        return best or (min(self.price_breaks, key=lambda p: p.quantity) if self.price_breaks else None)


def _http_json(url: str, *, data: dict | None = None, form: dict | None = None, headers: dict | None = None) -> dict:
    body = None
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})}
    if data is not None:
        body = json.dumps(data).encode()
        hdrs["Content-Type"] = "application/json"
    elif form is not None:
        body = urllib.parse.urlencode(form).encode()
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        raise DistributorError(f"HTTP {e.code} from {urllib.parse.urlsplit(url).netloc}: {detail}") from e
    except urllib.error.URLError as e:
        raise DistributorError(f"could not reach {urllib.parse.urlsplit(url).netloc}: {e.reason}") from e


def _price(text: object) -> float | None:
    """Parse prices such as ``"$0.10"``, ``"R$ 1,23"``, ``"1.234,56 €"`` or ``0.1``."""
    if isinstance(text, (int, float)):
        return float(text)
    s = re.sub(r"[^\d.,]", "", str(text or ""))
    if not s:
        return None
    if "," in s and "." in s:
        # whichever separator comes last is the decimal one
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        whole, frac = s.rsplit(",", 1)
        thousands = len(frac) == 3 and whole not in ("", "0") and "," not in whole[-4:-3]
        s = s.replace(",", "") if thousands else s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _int(v: object) -> int | None:
    m = re.search(r"\d[\d,.]*", str(v or ""))
    return int(re.sub(r"\D", "", m.group())) if m else None


# --------------------------------------------------------------------------- Mouser


class Mouser:
    name = "mouser"
    base = "https://api.mouser.com/api/v1"

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    @classmethod
    def from_env(cls) -> Mouser | None:
        key = config.get("MOUSER_API_KEY")
        return cls(key) if key else None

    def _post(self, path: str, payload: dict) -> list[Part]:
        data = _http_json(f"{self.base}{path}?apiKey={urllib.parse.quote(self.api_key)}", data=payload)
        errors = [e.get("Message") or e.get("Code") for e in data.get("Errors") or []]
        if errors:
            raise DistributorError("Mouser: " + "; ".join(str(e) for e in errors))
        return [self._part(p) for p in (data.get("SearchResults") or {}).get("Parts") or []]

    def search(self, keyword: str, limit: int = 10) -> list[Part]:
        return self._post(
            "/search/keyword",
            {"SearchByKeywordRequest": {"keyword": keyword, "records": min(limit, 50), "startingRecord": 0}},
        )

    def lookup(self, part_number: str) -> list[Part]:
        """Look up by Mouser or manufacturer part number."""
        return self._post(
            "/search/partnumber",
            {"SearchByPartRequest": {"mouserPartNumber": part_number, "partSearchOptions": "None"}},
        )

    @staticmethod
    def _part(p: dict) -> Part:
        breaks = []
        for pb in p.get("PriceBreaks") or []:
            price = _price(pb.get("Price"))
            if price is not None:
                breaks.append(PriceBreak(int(pb.get("Quantity") or 1), price, pb.get("Currency") or ""))
        attrs: dict[str, str] = {}
        for a in p.get("ProductAttributes") or []:
            k, v = a.get("AttributeName"), a.get("AttributeValue")
            if k and v:
                attrs[k] = f"{attrs[k]}, {v}" if k in attrs else v
        return Part(
            source="mouser",
            distributor_pn=p.get("MouserPartNumber") or "",
            mpn=p.get("ManufacturerPartNumber") or "",
            manufacturer=p.get("Manufacturer") or "",
            description=p.get("Description") or "",
            datasheet_url=p.get("DataSheetUrl") or "",
            product_url=p.get("ProductDetailUrl") or "",
            stock=_int(p.get("AvailabilityInStock") or p.get("Availability")),
            lifecycle=p.get("LifecycleStatus") or "",
            package=attrs.get("Package / Case", ""),
            min_order_qty=_int(p.get("Min")),
            price_breaks=breaks,
            attributes=attrs,
        )


# --------------------------------------------------------------------------- DigiKey


class DigiKey:
    name = "digikey"
    base = "https://api.digikey.com"

    def __init__(self, client_id: str, client_secret: str, site: str = "US", currency: str = "USD", language: str = "en") -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.locale = {
            "X-DIGIKEY-Locale-Site": site,
            "X-DIGIKEY-Locale-Currency": currency,
            "X-DIGIKEY-Locale-Language": language,
        }
        self.currency = currency
        self._token: tuple[str, float] | None = None

    @classmethod
    def from_env(cls) -> DigiKey | None:
        cid, secret = config.get("DIGIKEY_CLIENT_ID"), config.get("DIGIKEY_CLIENT_SECRET")
        if not (cid and secret):
            return None
        return cls(
            cid,
            secret,
            config.get("DIGIKEY_LOCALE_SITE") or "US",
            config.get("DIGIKEY_LOCALE_CURRENCY") or "USD",
            config.get("DIGIKEY_LOCALE_LANGUAGE") or "en",
        )

    def _headers(self) -> dict:
        if self._token is None or self._token[1] < time.time() + 30:
            data = _http_json(
                f"{self.base}/v1/oauth2/token",
                form={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "client_credentials",
                },
            )
            if "access_token" not in data:
                raise DistributorError(f"DigiKey sign-in failed: {data}")
            self._token = (data["access_token"], time.time() + float(data.get("expires_in", 600)))
        return {"Authorization": f"Bearer {self._token[0]}", "X-DIGIKEY-Client-Id": self.client_id, **self.locale}

    def search(self, keyword: str, limit: int = 10) -> list[Part]:
        data = _http_json(
            f"{self.base}/products/v4/search/keyword",
            data={"Keywords": keyword, "Limit": min(limit, 50), "Offset": 0},
            headers=self._headers(),
        )
        products = list(data.get("ExactMatches") or []) + list(data.get("Products") or [])
        seen, parts = set(), []
        for p in products:
            part = self._part(p)
            if part.distributor_pn not in seen:
                seen.add(part.distributor_pn)
                parts.append(part)
        return parts[:limit]

    def lookup(self, part_number: str) -> list[Part]:
        """Look up by DigiKey or manufacturer part number."""
        url = f"{self.base}/products/v4/search/{urllib.parse.quote(part_number, safe='')}/productdetails"
        try:
            data = _http_json(url, headers=self._headers())
        except DistributorError as e:
            if "HTTP 404" in str(e):
                return []
            raise
        return [self._part(data["Product"])] if data.get("Product") else []

    def _part(self, p: dict) -> Part:
        variations = p.get("ProductVariations") or []
        # prefer cut tape / bulk over full reels for prototype quantities
        def rank(v: dict) -> int:
            name = ((v.get("PackageType") or {}).get("Name") or "").lower()
            return 0 if "cut tape" in name else 1 if "reel" not in name else 2

        var = min(variations, key=rank) if variations else {}
        breaks = [
            PriceBreak(int(b.get("BreakQuantity") or 1), float(b.get("UnitPrice") or 0), self.currency)
            for b in var.get("StandardPricing") or []
        ]
        attrs = {
            (x.get("ParameterText") or ""): (x.get("ValueText") or "")
            for x in p.get("Parameters") or []
            if x.get("ParameterText")
        }
        desc = p.get("Description") or {}
        return Part(
            source="digikey",
            distributor_pn=var.get("DigiKeyProductNumber") or "",
            mpn=p.get("ManufacturerProductNumber") or "",
            manufacturer=(p.get("Manufacturer") or {}).get("Name") or "",
            description=desc.get("ProductDescription") or desc.get("DetailedDescription") or "",
            datasheet_url=p.get("DatasheetUrl") or "",
            product_url=p.get("ProductUrl") or "",
            stock=p.get("QuantityAvailable"),
            lifecycle=(p.get("ProductStatus") or {}).get("Status") or "",
            package=attrs.get("Package / Case", ""),
            min_order_qty=var.get("MinimumOrderQuantity"),
            price_breaks=breaks,
            attributes=attrs,
        )


# --------------------------------------------------------------------------- facade


def configured() -> list:
    return [c for c in (Mouser.from_env(), DigiKey.from_env()) if c is not None]


def status() -> dict:
    return {
        "mouser": "configured" if Mouser.from_env() else "set MOUSER_API_KEY",
        "digikey": "configured" if DigiKey.from_env() else "set DIGIKEY_CLIENT_ID and DIGIKEY_CLIENT_SECRET",
    }


def _clients(source: str | None) -> list:
    clients = configured()
    if source:
        clients = [c for c in clients if c.name == source.lower()]
    if not clients:
        raise DistributorError(
            "no distributor API is configured"
            + (f" for {source}" if source else "")
            + f". Put MOUSER_API_KEY and/or DIGIKEY_CLIENT_ID + DIGIKEY_CLIENT_SECRET in {config.config_path()}"
            " or in the MCP server's env."
        )
    return clients


def search(keyword: str, limit: int = 10, source: str | None = None) -> dict:
    """Search every configured distributor; one failing does not hide the others' results."""
    out: dict = {"results": [], "errors": {}}
    for c in _clients(source):
        try:
            out["results"] += [p.to_dict(attributes=False) for p in c.search(keyword, limit)]
        except DistributorError as e:
            out["errors"][c.name] = str(e)
    return out


def lookup(part_number: str, source: str | None = None) -> list[Part]:
    parts: list[Part] = []
    errors = []
    for c in _clients(source):
        try:
            parts += c.lookup(part_number)
        except DistributorError as e:
            errors.append(f"{c.name}: {e}")
    if not parts and errors:
        raise DistributorError("; ".join(errors))
    exact = [p for p in parts if part_number.upper() in (p.mpn.upper(), p.distributor_pn.upper())]
    return exact or parts
