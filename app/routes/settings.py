"""Organisation settings and the audit trail."""

from fastapi import APIRouter, Request

from app.models.audit_log import AuditAction, AuditEntity
from app.schemas.audit import AuditLogOut
from app.schemas.common import PaginatedData
from app.schemas.settings import (
    LocationCheck,
    OrgSettingsOut,
    OrgSettingsUpdate,
    TrackingStartUpdate,
)
from app.services import audit as audit_service
from app.services import leave as leave_service
from app.services import settings as settings_service
from app.utils.deps import CurrentActor, CurrentAdmin, CurrentUser, Pagination
from app.utils.network import client_ip, location_view, network_view
from app.utils.response import success

router = APIRouter(tags=["settings"])


@router.get("/settings", summary="Organisation settings")
async def get_settings(_: CurrentUser) -> dict:
    return success(OrgSettingsOut.from_document(await settings_service.get_settings()))


@router.get("/settings/network", summary="The office network, seen from this request")
async def network_check(request: Request, _: CurrentAdmin) -> dict:
    """The address this service judges the caller by, and whether it is allowed.

    An administrator standing on the office Wi-Fi opens this to find out what to
    put in the allowlist. Reporting it from the API rather than sending them to
    a "what is my IP" site matters: with a proxy in front, the address the site
    shows them and the address the allowlist compares are different, and pasting
    the wrong one locks the office out of punching.
    """
    return success(await network_view(client_ip(request)))


@router.post("/settings/location-check", summary="How far this computer is from the office")
async def location_check(payload: LocationCheck, _: CurrentUser) -> dict:
    """Compare a browser's reading with the office, by the rule punches use.

    Any signed-in person may ask: it answers a distance, never the office's
    coordinates, and it is how somebody standing in the office can see whether
    a punch from this computer would be accepted before they need it to be.
    """
    org_settings = await settings_service.get_settings()
    return success(
        location_view(
            org_settings, payload.latitude, payload.longitude, payload.accuracy_metres
        )
    )


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
