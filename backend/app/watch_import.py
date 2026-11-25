"""Parse an uploaded Excel/CSV sheet of custom KPIs (Source name, KPI name, Link)."""

from __future__ import annotations

import csv
import io
import re

import openpyxl

COLUMNS = (("source_name", "Source name"), ("kpi_name", "KPI name"), ("url", "Link"))
MAX_ROWS = 5000


class SheetError(ValueError):
    """The sheet cannot be read or lacks a required column."""


def _norm(h) -> str:
    """Header key: case and all whitespace ignored ("SourceName" == "Source  name")."""
    return re.sub(r"\s+", "", str(h or "").replace("﻿", "")).lower()


def _cell(v) -> str:
    return "" if v is None else str(v).strip()



def _read_limit() -> int:
    """Header plus one row too many: enough to reject an oversized sheet without reading it all."""
    return MAX_ROWS + 2

_HYPERLINK_FORMULA = re.compile(r'^=\s*HYPERLINK\(\s*"((?:[^"]|"")*)"', re.IGNORECASE)


def _xlsx_rows(data: bytes) -> list[list]:
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        try:
            rows = []
            for r in wb.worksheets[0].iter_rows(values_only=True):
                rows.append(list(r))
                if len(rows) >= _read_limit():
                    break
            return rows
        finally:
            wb.close()
    except Exception as e:  # noqa: BLE001 - any parser failure is a bad upload
        raise SheetError("unreadable Excel file") from e


def _xlsx_link_targets(data: bytes, col: int, last_row: int) -> dict[int, str]:
    """URL behind each Link cell that is a hyperlink (display text such as "Click here")
    or a =HYPERLINK("url", "text") formula, keyed by sheet row.

    Read-only mode exposes neither, so this loads the sheet normally with formulas kept;
    it runs only after _xlsx_rows has bounded the row count."""
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=False)
    except Exception as e:  # noqa: BLE001
        raise SheetError("unreadable Excel file") from e
    try:
        out: dict[int, str] = {}
        ws = wb.worksheets[0]
        for (cell,) in ws.iter_rows(min_row=2, max_row=last_row, min_col=col + 1, max_col=col + 1):
            link = cell.hyperlink
            if link is not None and link.target:
                out[cell.row] = link.target
            elif isinstance(cell.value, str) and (m := _HYPERLINK_FORMULA.match(cell.value)):
                out[cell.row] = m.group(1).replace('""', '"')
        return out
    finally:
        wb.close()


def _csv_rows(data: bytes) -> list[list]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    first = text.split("\n", 1)[0]
    delim = max(",;\t", key=first.count) if any(d in first for d in ",;\t") else ","
    try:
        rows = []
        for r in csv.reader(io.StringIO(text), delimiter=delim):
            rows.append(list(r))
            if len(rows) >= _read_limit():
                break
        return rows
    except csv.Error as e:
        raise SheetError("unreadable CSV file") from e


def parse_sheet(filename: str, data: bytes) -> list[dict]:
    """Rows as {row, source_name, kpi_name, url}; row is the sheet row (header = 1).

    Blank rows are skipped. Raises SheetError for an unknown type or a missing column.
    """
    name = filename.lower()
    is_xlsx = name.endswith(".xlsx")
    if is_xlsx:
        grid = _xlsx_rows(data)
    elif name.endswith(".csv"):
        grid = _csv_rows(data)
    else:
        raise SheetError("unsupported file type (use .xlsx or .csv)")
    if not grid:
        raise SheetError("empty file")
    header = [_norm(h) for h in grid[0]]
    index: dict[str, int] = {}
    for key, label in COLUMNS:
        if _norm(label) not in header:
            raise SheetError(f"missing column: {label}")
        index[key] = header.index(_norm(label))
    if len(grid) - 1 > MAX_ROWS:
        raise SheetError(f"too many rows (max {MAX_ROWS})")
    links = _xlsx_link_targets(data, index["url"], len(grid)) if is_xlsx and len(grid) > 1 else {}
    rows = []
    for n, raw in enumerate(grid[1:], start=2):
        vals = {k: _cell(raw[i]) if i < len(raw) else "" for k, i in index.items()}
        if n in links:
            vals["url"] = links[n].strip()
        if not any(vals.values()):
            continue
        rows.append({"row": n, **vals})
    return rows
