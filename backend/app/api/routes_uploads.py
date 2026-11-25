"""Manual-source file uploads: the file is stored and queued as a held run for review."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.api.util import iso
from app.connectors.registry import resolve_connector
from app.core import raw_store
from app.models import Run, Source, Upload

router = APIRouter()

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
_CHUNK = 1024 * 1024
QUEUED_CONFLICT = "a check for this source is already queued"


@router.post("/uploads", status_code=202)
def create_upload(
    source: str = Form(...),
    who: str = Form(...),
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
):
    src = session.scalars(select(Source).where(Source.slug == source)).one_or_none()
    if src is None:
        raise HTTPException(404, "source not found")
    if src.access != "manual":
        raise HTTPException(422, "uploads are only accepted for manual sources")
    try:
        connector = resolve_connector(session, src.slug)
    except KeyError:
        raise HTTPException(422, "source has no connector") from None

    run = Run(source_id=src.id, status="queued", trigger="upload")
    session.add(run)
    dest = None
    try:
        session.flush()  # the id names the raw file
        dest = raw_store.path_for(src.slug, run.id, connector.raw_suffix)
        _save(file, dest)
        run.raw_path = str(dest)
        session.add(Upload(source_id=src.id, who=who, filename=file.filename or "upload",
                           run_id=run.id))
        session.commit()
    except BaseException as e:
        if dest is not None:
            dest.unlink(missing_ok=True)
        session.rollback()
        if isinstance(e, IntegrityError) and "uq_runs_one_queued_per_source" in str(e.orig):
            raise HTTPException(409, QUEUED_CONFLICT) from None
        raise
    return {"run_id": run.id}


def _save(file: UploadFile, dest) -> None:
    size = 0
    with dest.open("wb") as out:
        while chunk := file.file.read(_CHUNK):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                raise HTTPException(413, "file too large (max 50 MB)")
            out.write(chunk)


@router.get("/uploads")
def list_uploads(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    session: Session = Depends(get_session),
):
    total = session.scalar(select(func.count()).select_from(Upload))
    rows = session.execute(
        select(Upload, Source.slug, Run.status)
        .join(Source, Source.id == Upload.source_id)
        .outerjoin(Run, Run.id == Upload.run_id)
        .order_by(Upload.id.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    ).all()
    return {
        "items": [
            {"id": u.id, "source": slug, "who": u.who, "filename": u.filename,
             "created_at": iso(u.created_at), "run_id": u.run_id, "run_status": status}
            for u, slug, status in rows
        ],
        "total": total,
    }
