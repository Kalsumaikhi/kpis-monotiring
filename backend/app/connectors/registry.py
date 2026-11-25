from __future__ import annotations

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from app.connectors.base import Connector

CONNECTORS: dict[str, Connector] = {}


def get_connector(slug: str) -> Connector:
    try:
        return CONNECTORS[slug]
    except KeyError:
        raise KeyError(f"unknown connector: {slug}") from None


def resolve_connector(session: Session, slug: str) -> Connector:
    """Static connector by slug, else a WatchConnector for a DB source with access='watch'."""
    if slug in CONNECTORS:
        return CONNECTORS[slug]
    from app.connectors.watch.connector import WatchConnector
    from app.models import Source

    row = session.execute(
        select(Source.id, Source.name, Source.access).where(Source.slug == slug)
    ).first()
    if row is None or row.access != "watch":
        raise KeyError(f"unknown connector: {slug}")
    return WatchConnector(row.id, slug, row.name, sessionmaker(bind=session.get_bind()))


def sync_sources(session: Session) -> None:
    """Upsert one sources row per connector; cron and enabled of existing rows stay."""
    for c in CONNECTORS.values():
        session.execute(
            text(
                """
                INSERT INTO sources (slug, name, area, access)
                VALUES (:slug, :name, :area, :access)
                ON CONFLICT (slug) DO UPDATE
                SET name = EXCLUDED.name, area = EXCLUDED.area, access = EXCLUDED.access
                """
            ),
            {"slug": c.slug, "name": c.name, "area": c.area, "access": c.access},
        )
    session.commit()


def _register() -> None:
    # imported here: connector modules import app.connectors.base, not this module
    from app.connectors.imf_weo import ImfWeo
    from app.connectors.sipri_milex import SipriMilex
    from app.connectors.worldbank_wdi import WorldBankWDI

    for c in (WorldBankWDI(), ImfWeo(), SipriMilex()):
        CONNECTORS[c.slug] = c


_register()
