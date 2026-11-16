"""Soft delete, trash listing and restore."""

import shutil
import time

from fastapi import APIRouter
from pydantic import BaseModel, Field

from api.maintenance import sweep_trash
import jobs
from api.common import TRASH, read_artifact
from jsonio import atomic_write_json, load_json
from pipeline import SESSIONS, session_dir

router = APIRouter()


class BulkDelete(BaseModel):
    ids: list[str] = Field(min_length=1)


@router.post("/api/sessions/delete")
def delete_sessions(req: BulkDelete):
    """Soft-delete: move each session into .trash/ (restorable) rather than
    deleting outright. Trash is purged lazily (see sweep_trash) after
    TRASH_RETENTION_S."""
    deleted, errors = [], []
    for sid in req.ids:
        try:
            d = session_dir(sid)  # rejects path traversal
        except ValueError:
            errors.append({"id": sid, "error": "bad session id"})
            continue
        if not d.exists():
            errors.append({"id": sid, "error": "not found"})
            continue
        if jobs.is_busy(d):  # moving the folder out from under a running job breaks it
            errors.append({"id": sid, "error": "session is still processing"})
            continue
        meta_f = d / "meta.json"
        meta = load_json(meta_f)
        meta["deleted_at"] = time.time()
        atomic_write_json(meta_f, meta)
        TRASH.mkdir(parents=True, exist_ok=True)
        dest = TRASH / sid
        if dest.exists():  # stale leftover from a prior delete of the same id
            shutil.rmtree(dest)
        shutil.move(str(d), str(dest))
        deleted.append(sid)
    return {"deleted": deleted, "errors": errors}


@router.get("/api/sessions/trash")
def list_trash():
    sweep_trash()
    out = []
    if TRASH.exists():
        for d in sorted(TRASH.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not d.is_dir():
                continue
            meta = read_artifact(d, "meta.json")
            out.append({
                "session_id": d.name,
                "label": meta.get("label"),
                "filename": meta.get("filename"),
                "deleted_at": meta.get("deleted_at"),
            })
    return out


@router.post("/api/sessions/restore")
def restore_sessions(req: BulkDelete):
    restored, errors = [], []
    for sid in req.ids:
        try:
            session_dir(sid)  # validates id shape, rejects traversal
        except ValueError:
            errors.append({"id": sid, "error": "bad session id"})
            continue
        d = TRASH / sid
        if not d.exists():
            errors.append({"id": sid, "error": "not found in trash"})
            continue
        meta_f = d / "meta.json"
        meta = load_json(meta_f)
        meta.pop("deleted_at", None)
        atomic_write_json(meta_f, meta)
        dest = SESSIONS / sid
        if dest.exists():  # stale leftover from a prior restore of the same id
            shutil.rmtree(dest)
        shutil.move(str(d), str(dest))
        restored.append(sid)
    return {"restored": restored, "errors": errors}
