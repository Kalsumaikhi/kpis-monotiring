"""SIPRI Military Expenditure Database connector.

Verified 2026-11-24 against the live site:
- https://www.sipri.org/databases/milex links the workbook as a protocol-relative href,
  e.g. `//www.sipri.org/sites/default/files/SIPRI-Milex-data-1949-2025_v1.2.xlsx`. The file name
  changes every release, so download() reads the page, takes the first href matching
  `SIPRI-Milex-data-.*\\.xlsx`, resolves it against the page URL (https) and fetches it (~0.9 MB).
  No matching link raises RuntimeError.
- Terms (https://www.sipri.org/about/terms-and-conditions): "SIPRI data may be freely downloaded,
  cited and used for statistical or other analytical purposes provided that such use is in line
  with SIPRI's fair-use policy"; nothing forbids automated download. Fair use = non-commercial
  use and less than 10 per cent of the data set reproduced; commercial use (explicitly including
  "internal tools aimed at gaining a business advantage") needs a licence. Attribution is
  required: 'Information from the Stockholm International Peace Research Institute (SIPRI)
  Military Expenditure Database, https://doi.org/10.55163/CQGC9685'.
- Workbook sheets: "Front page" (a drawing only, no cells), "Regional totals", two "Local currency
  ..." sheets, "Constant (2024) US$", "Current US$", "Share of GDP", "Per capita",
  "Share of Govt. spending", "Footnotes". Imported: the five data sheets; regional totals and local
  currency sheets (per-country currencies) are not.
- Layout: title/notes rows, then a header row whose first cell is "Country" (row 6-8, varies by
  sheet) and has a "Notes" column, optionally "Reporting year" or a blank column, and one int
  column per year. Region/subregion heading rows have a name and no values; their names also
  appear in "Regional totals". Cells are numbers, "...", "…" (unavailable), "xxx" (country did
  not exist), "" or empty; only numbers become rows. Share sheets hold fractions (0.047 = 4.7%).

Design choices:
- Indicator code = slugified sheet name with the parenthesised base year removed and "US$" read
  as "usd" ("Constant (2024) US$" -> "constant-usd"), so a new base year does not rename it. The
  indicator name is the sheet name as is.
- Country is the name as written (ISO3 mapping is Plan 3). dataset = "SIPRI-MILEX".
- complete = all five expected codes found. New sheets are imported too, unless named like
  "Front page", "Regional totals", "Footnotes" or "Local currency ...".
- vintage: the xlsx is the only raw file, so it is read from inside it: the citation text in the
  front-page drawing ("SIPRI Military Expenditure Database 2026") plus the newest year column,
  e.g. "SIPRI Military Expenditure Database 2026 (data to 2025)". Without the citation only
  "SIPRI Military Expenditure Database (data to 2025)".
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from urllib.parse import urljoin

import httpx
import openpyxl

from app.connectors.base import IndicatorMeta, ParseResult
from app.core.canonical import RawRow

PAGE = "https://www.sipri.org/databases/milex"
LINK_RE = re.compile(r"""href=["']([^"']*SIPRI-Milex-data-[^"']*\.xlsx)["']""", re.I)
TIMEOUT = 120.0
DATASET = "SIPRI-MILEX"
EXPECTED = {"constant-usd", "current-usd", "share-of-gdp", "per-capita", "share-of-govt-spending"}
SKIP_SHEETS = re.compile(r"^(front page|regional totals|footnotes|local currency)", re.I)
UNITS = {
    "constant-usd": "US$ m (constant prices)",
    "current-usd": "US$ m (current prices)",
    "share-of-gdp": "share of GDP (fraction)",
    "per-capita": "current US$ per person",
    "share-of-govt-spending": "share of government spending (fraction)",
}


def _code(sheet: str) -> str:
    s = re.sub(r"\(\s*\d{4}\s*\)", "", sheet)
    s = re.sub(r"US\s*\$", "usd", s, flags=re.I)
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _year(v) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int) or (isinstance(v, float) and v.is_integer()):
        v = int(v)
    elif isinstance(v, str) and v.strip().isdigit():
        v = int(v.strip())
    else:
        return None
    return v if 1800 <= v <= 2200 else None


class SipriMilex:
    slug = "sipri-milex"
    name = "SIPRI Military Expenditure Database"
    area = "military"
    access = "url"
    raw_suffix = ".xlsx"

    def download(self, client: httpx.Client, dest: Path) -> None:
        page = client.get(PAGE, timeout=TIMEOUT, follow_redirects=True)
        page.raise_for_status()
        m = LINK_RE.search(page.text)
        if not m:
            raise RuntimeError(f"no SIPRI-Milex-data xlsx link found on {PAGE}")
        url = urljoin(str(page.url), m.group(1))
        if url.startswith("http://"):
            url = "https://" + url[len("http://"):]
        tmp = dest.with_name(dest.name + ".part")
        with client.stream("GET", url, timeout=TIMEOUT, follow_redirects=True) as r:
            r.raise_for_status()
            with tmp.open("wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
        tmp.replace(dest)

    def parse(self, raw: Path) -> ParseResult:
        wb = openpyxl.load_workbook(raw, read_only=True, data_only=True)
        try:
            regions = self._regions(wb)
            rows: list[RawRow] = []
            metas: list[IndicatorMeta] = []
            warnings: list[str] = []
            newest = 0
            for sheet in wb.sheetnames:
                if SKIP_SHEETS.match(sheet.strip()):
                    continue
                code = _code(sheet)
                got = self._sheet(wb[sheet], code, regions)
                if got is None:
                    warnings.append(f"sheet {sheet!r} has no 'Country' header row; skipped")
                    continue
                sheet_rows, years = got
                rows.extend(sheet_rows)
                newest = max(newest, max(years, default=0))
                metas.append(IndicatorMeta(code=code, name=sheet.strip(), unit=UNITS.get(code)))
        finally:
            wb.close()
        missing = sorted(EXPECTED - {m.code for m in metas})
        if missing:
            warnings.append("incomplete: missing sheets for " + ", ".join(missing))
        return ParseResult(
            rows=rows, complete=not missing, indicators=metas,
            vintage=self._vintage(raw, newest), warnings=warnings,
        )

    @staticmethod
    def _regions(wb) -> set[str]:
        out: set[str] = set()
        if "Regional totals" not in wb.sheetnames:
            return out
        started = False
        for r in wb["Regional totals"].iter_rows(values_only=True):
            a = r[0] if r else None
            if not isinstance(a, str) or not a.strip():
                continue
            if started:
                out.add(a.strip())
            elif a.strip() == "Region":
                started = True
        return out

    @staticmethod
    def _sheet(ws, code: str, regions: set[str]):
        header: dict[int, int] | None = None
        out: list[RawRow] = []
        for r in ws.iter_rows(values_only=True):
            if header is None:
                if r and isinstance(r[0], str) and r[0].strip() == "Country":
                    header = {i: y for i, c in enumerate(r) if (y := _year(c)) is not None}
                continue
            name = r[0].strip() if r and isinstance(r[0], str) else ""
            if not name or name in regions:
                continue
            for i, year in header.items():
                v = r[i] if i < len(r) else None
                # "...", "…", "xxx", "" and None are not numbers: no row
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    continue
                out.append(RawRow(dataset=DATASET, indicator=code, country=name, period=str(year), value=v))
        if header is None:
            return None
        return out, set(header.values())

    @staticmethod
    def _vintage(raw: Path, newest: int) -> str:
        edition = ""
        try:
            with zipfile.ZipFile(raw) as z:
                for n in z.namelist():
                    if n.startswith("xl/drawings/") and n.endswith(".xml"):
                        m = re.search(r"Military Expenditure Database (\d{4})", z.read(n).decode("utf-8", "replace"))
                        if m:
                            edition = f" {m.group(1)}"
                            break
        except (zipfile.BadZipFile, OSError):
            pass
        tail = f" (data to {newest})" if newest else ""
        return f"SIPRI Military Expenditure Database{edition}{tail}"
