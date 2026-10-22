"""Name the place on punches that were recorded before there was a name for it.

    python scripts/backfill_places.py            # report only
    python scripts/backfill_places.py --apply

Punches have always stored coordinates, but the city beside them is new, and
without a Google key the earlier address lookup answered nothing at all — so
every punch already in the database shows an IP address where an administrator
wants to read "Surat". This walks those punches, resolves each distinct location
once through the ordinary geocoding path, and writes the answer back.

Safe to re-run. It only touches punches that have coordinates and no city, and
it shares the coordinate cache with the live service, so a whole office costs a
single lookup. Nothing here changes attendance: no status, no minute, no punch
time is written.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.logging import get_logger, setup_logging  # noqa: E402
from app.db.mongo import (  # noqa: E402
    Collections,
    close_mongo_connection,
    connect_to_mongo,
    get_collection,
)
from app.services import geocode  # noqa: E402

logger = get_logger("backfill_places")

#: Nominatim asks for no more than one request a second. The cache means only a
#: distinct location costs a request, but a database spanning several cities
#: would still burst without this.
PAUSE_BETWEEN_LOOKUPS_SECONDS = 1.1


def punches(record: dict) -> list[dict]:
    events = []
    for session in record.get("sessions") or []:
        for key in ("punch_in", "punch_out"):
            event = session.get(key)
            if event:
                events.append(event)
    return events


def needs_a_place(event: dict) -> bool:
    return (
        event.get("latitude") is not None
        and event.get("longitude") is not None
        and not event.get("city")
    )


async def run(*, apply: bool) -> None:
    await connect_to_mongo()
    attendance = get_collection(Collections.ATTENDANCE)

    records = await attendance.find({"sessions.0": {"$exists": True}}).to_list(length=None)

    resolved: dict[str, geocode.Place] = {}
    days_touched = 0
    punches_touched = 0
    lookups = 0

    for record in records:
        events = [event for event in punches(record) if needs_a_place(event)]
        if not events:
            continue

        changed = False
        for event in events:
            key = geocode.cache_key(event["latitude"], event["longitude"])
            if key not in resolved:
                if lookups:
                    await asyncio.sleep(PAUSE_BETWEEN_LOOKUPS_SECONDS)
                lookups += 1
                resolved[key] = await geocode.reverse_place(
                    event["latitude"], event["longitude"]
                )
            place = resolved[key]
            if not place.city:
                continue
            event["city"] = place.city
            event["address"] = event.get("address") or place.address
            punches_touched += 1
            changed = True

        if not changed:
            continue
        days_touched += 1
        if apply:
            await attendance.update_one(
                {"_id": record["_id"]}, {"$set": {"sessions": record["sessions"]}}
            )

    verb = "Updated" if apply else "Would update"
    logger.info(
        "%s %s punch(es) across %s day(s) from %s distinct location(s).",
        verb,
        punches_touched,
        days_touched,
        lookups,
    )
    if not apply and punches_touched:
        logger.info("Nothing was written. Re-run with --apply to save these.")

    await close_mongo_connection()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the resolved places. Without this the script only reports.",
    )
    arguments = parser.parse_args()

    setup_logging()
    asyncio.run(run(apply=arguments.apply))


if __name__ == "__main__":
    main()
