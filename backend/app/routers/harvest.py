from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services import harvest as harvest_service
from app.services.pipeline import get_domain

router = APIRouter(tags=["采集位点"])


class HarvestBody(BaseModel):
    domain_id: int = Field(..., description="主题域编号")


class HarvestRunError(HTTPException):
    pass


def _domain_or_404(domain_id: int) -> dict:
    domain = get_domain(domain_id)
    if not domain:
        raise HTTPException(status_code=404, detail="主题域不存在")
    return domain


@router.get("/harvest/watermark")
def get_watermark(domain_id: int = Query(..., description="主题域编号")):
    _domain_or_404(domain_id)
    return harvest_service.watermark_state(domain_id)


@router.get("/harvest/jobs")
def list_harvest_jobs(
    domain_id: int | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
):
    if domain_id is not None:
        _domain_or_404(domain_id)
    return harvest_service.list_harvest_jobs(domain_id, limit)


@router.get("/harvest/jobs/{job_id}")
def get_harvest_job(job_id: int):
    try:
        return harvest_service.get_harvest_job(job_id)
    except harvest_service.HarvestError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/harvest/run")
def run_harvest(body: HarvestBody):
    _domain_or_404(body.domain_id)
    try:
        return harvest_service.run_harvest(body.domain_id)
    except harvest_service.HarvestBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except harvest_service.HarvestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
