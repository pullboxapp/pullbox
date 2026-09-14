"""Instance-local catalog controls. No metadata proxy routes."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from pullbox.api.deps import AuthenticatedUser, InteractiveOperatorUser
from pullbox.services.catalog.service import CatalogStatus

router = APIRouter(prefix="/catalog", tags=["catalog"])


@router.get("")
async def catalog_status(user: AuthenticatedUser) -> CatalogStatus:
    from pullbox.services.catalog.service import get_catalog_service

    return get_catalog_service().status()


@router.post("/sync", status_code=202)
async def sync_catalog(user: InteractiveOperatorUser) -> dict[str, str]:
    from pullbox.core.scheduler import get_scheduler

    status = get_scheduler().run_task_now("catalog_update")
    if status is None:
        raise HTTPException(503, "The catalog task is unavailable. Restart Pullbox and retry.")
    return {"status": status}


class CatalogPreferences(BaseModel):
    automatic_updates: bool


@router.patch("/preferences")
async def catalog_preferences(
    body: CatalogPreferences, user: InteractiveOperatorUser
) -> CatalogStatus:
    from pullbox.services.catalog.service import get_catalog_service

    service = get_catalog_service()
    await service.set_automatic_updates(body.automatic_updates)
    return service.status()
