"""Admin update API; execution is delegated to a separate systemd unit."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from .auth import require_admin
from .updater import UpdateError, launch_update, read_job

router = APIRouter(prefix="/api/v1/admin/update", tags=["update"])

class UpdateRequest(BaseModel):
    force: bool = False

@router.post("")
def request_update(value: UpdateRequest, _device=Depends(require_admin)):
    try: job_id = launch_update(force=value.force)
    except UpdateError as exc: raise HTTPException(503, str(exc))
    return {"job_id": job_id}

@router.get("/{job_id}")
def update_status(job_id: str, _device=Depends(require_admin)):
    try: value = read_job(job_id)
    except UpdateError as exc: raise HTTPException(404, str(exc))
    value["log"] = value.get("log", [])[-50:]
    return value

