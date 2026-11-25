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
    return re.sub(r"\s+", " ", str(h or "").replace("﻿", "")).strip().lower()


def _cell(v) -> str:
    return "" if v is None else str(v).strip()


def _xlsx_rows(data: bytes) -> list[list]:
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        ws = wb.worksheets[0]
        return [list(r) for r in ws.iter_rows(values_only=True)]
    except Exception as e:  # noqa: BLE001 - any parser failure is a bad upload
        raise SheetError("unreadable Excel file") from e


def _csv_rows(data: bytes) -> list[list]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    first = text.split("\n", 1)[0]
    delim = max(",;\t", key=first.count) if any(d in first for d in ",;\t") else ","
    return [list(r) for r in csv.reader(io.StringIO(text), delimiter=delim)]


def parse_sheet(filename: str, data: bytes) -> list[dict]:
    """Rows as {row, source_name, kpi_name, url}; row is the sheet row (header = 1).

    Blank rows are skipped. Raises SheetError for an unknown type or a missing column.
    """
    name = filename.lower()
    if name.endswith(".xlsx"):
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
    rows = []
    for n, raw in enumerate(grid[1:], start=2):
        vals = {k: _cell(raw[i]) if i < len(raw) else "" for k, i in index.items()}
        if not any(vals.values()):
            continue
        rows.append({"row": n, **vals})
    return rows
