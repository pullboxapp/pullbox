"""Catalog update scheduler integration."""

from pullbox.core.scheduler import get_current_task_trigger_type, scheduled_task


@scheduled_task(
    task_id="catalog_update",
    display_name="Local Catalog Update",
    trigger="cron",
    hour=6,
    minute=30,
    jitter=1800,
    misfire_grace_time=3600,
)
async def update_catalog() -> None:
    """Check daily after opt-in; manual runs also allow the first download."""
    from pullbox.services.catalog.service import get_catalog_service

    await get_catalog_service().sync(manual=get_current_task_trigger_type() == "manual")
