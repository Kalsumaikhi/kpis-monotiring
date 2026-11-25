"""Turn fetched pages and API responses into stable, comparable text."""

from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup

_DROP = ("script", "style", "noscript", "nav", "header", "footer", "aside", "form")
_WS = re.compile(r"[ \t\r\f\v ]+")


def _lines(text: str) -> str:
    out = []
    for line in text.splitlines():
        line = _WS.sub(" ", line).strip()
        if line:
            out.append(line)
    return "\n".join(out)


def extract_page(html: str, selector: str | None) -> tuple[str, str | None]:
    """Return (text, error). A selector that matches nothing gives ("", error)."""
    soup = BeautifulSoup(html, "html.parser")
    for el in soup(_DROP):
        el.decompose()
    if selector:
        try:
            matches = soup.select(selector)
        except Exception:  # noqa: BLE001 - invalid selector syntax
            return "", f"invalid selector: {selector}"
        if not matches:
            return "", f"selector matched nothing: {selector}"
        return _lines("\n".join(m.get_text("\n") for m in matches)), None
    root = soup.find("main") or soup.find("article") or soup.body or soup
    return _lines(root.get_text("\n")), None


def page_text(html: str, selector: str | None) -> str:
    return extract_page(html, selector)[0]


def flatten_json(data: Any, prefix: str = "") -> dict[str, str]:
    """Leaves of a JSON document by path, e.g. `a.b[0].c`; null leaves become "null"."""
    out: dict[str, str] = {}
    if isinstance(data, dict):
        for k, v in data.items():
            out.update(flatten_json(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(data, list):
        for i, v in enumerate(data):
            out.update(flatten_json(v, f"{prefix}[{i}]"))
    elif prefix == "" and not isinstance(data, (dict, list)):
        out[""] = _leaf(data)
    else:
        out[prefix] = _leaf(data)
    return out


def _leaf(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)
