"""Organisation settings and the audit trail."""

from fastapi import APIRouter

from app.models.audit_log import AuditAction, AuditEntity
from app.schemas.audit import AuditLogOut
from app.schemas.common import PaginatedData
from app.schemas.settings import (
    OrgSettingsOut,
    OrgSettingsUpdate,
    TrackingStartUpdate,
)
from app.services import audit as audit_service
from app.services import leave as leave_service
from app.services import settings as settings_service
from app.utils.deps import CurrentActor, CurrentAdmin, CurrentUser, Pagination
from app.utils.response import success

router = APIRouter(tags=["settings"])


@router.get("/settings", summary="Organisation settings")
async def get_settings(_: CurrentUser) -> dict:
    return success(OrgSettingsOut.from_document(await settings_service.get_settings()))




@router.patch("/settings", summary="Update organisation settings")
async def update_settings(
    payload: OrgSettingsUpdate, _: CurrentAdmin, actor: CurrentActor
) -> dict:
    updated = await settings_service.update_settings(payload, actor)
    return success(OrgSettingsOut.from_document(updated), message="Settings updated.")


@router.post("/settings/tracking-start", summary="Start counting attendance from a day")
async def set_tracking_start(
    payload: TrackingStartUpdate, _: CurrentAdmin, actor: CurrentActor
) -> dict:
    updated = await settings_service.set_tracking_start(payload.starts_on, actor)
    # Leave restarts with attendance: balances are zero on go-live day and
    # accrue from there, so nothing earned or taken before it carries over.
    await leave_service.restart_balances(await settings_service.tracking_start())
    return success(
        OrgSettingsOut.from_document(updated),
        message="Attendance and leave now count from " + updated["tracking_starts_on"] + ".",
    )


@router.get("/audit-logs", summary="Audit trail")
async def list_audit_logs(
    _: CurrentAdmin,
    pagination: Pagination,
    actor_id: str | None = None,
    entity: AuditEntity | None = None,
    entity_id: str | None = None,
    action: AuditAction | None = None,
) -> dict:
    items, total = await audit_service.list_logs(
        pagination,
        actor_id=actor_id,
        entity=entity.value if entity else None,
        entity_id=entity_id,
        action=action.value if action else None,
    )
    page = PaginatedData.build(
        [AuditLogOut.from_document(item) for item in items],
        page=pagination.page,
        page_size=pagination.page_size,
        total=total,
    )
    return success(page)
