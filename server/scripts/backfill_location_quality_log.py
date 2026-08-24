"""
Retroactively populate location_quality_log for existing `locations` rows,
using the same compute_quality_log_fields used at ingestion time
(app/api/locations.py — imported here, not reimplemented, so live ingestion
and this backfill can never disagree).

Run scripts/backfill_location_outliers.py --write FIRST if it hasn't been
run yet, so is_outlier reflects its final state before this script snapshots
it — this script copies Location.is_outlier as-is, it doesn't recompute it.

Idempotent: skips any location that already has a location_quality_log row
(so it's safe to re-run after new points arrive — only the new points get
logged). Never touches existing log rows.

Usage:
  # Dry run for one device:
  python scripts/backfill_location_quality_log.py --device-id 1

  # Dry run for every device:
  python scripts/backfill_location_quality_log.py --all-devices

  # Real write pass for one device:
  python scripts/backfill_location_quality_log.py --device-id 1 --write
"""

import argparse
import sys
import os
import logging

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.core.database import SessionLocal
from app.models.location import Location
from app.models.location_quality_log import LocationQualityLog
from app.models.device import Device
from app.api.locations import compute_quality_log_fields

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)


def process_device(db, device_id: int, write: bool) -> int:
    """
    Walk one device's location history chronologically, creating a
    location_quality_log row for every location that doesn't already have
    one. Returns the number of rows created (0 if fully caught up already).
    """
    already_logged_ids = {
        row[0] for row in
        db.query(LocationQualityLog.location_id)
        .filter(LocationQualityLog.device_id == device_id)
        .all()
    }

    locations = (
        db.query(Location)
        .filter(Location.device_id == device_id)
        .order_by(Location.timestamp.asc())
        .all()
    )

    created = 0
    prev1 = None
    for loc in locations:
        if loc.id not in already_logged_ids:
            fields = compute_quality_log_fields(
                prev1, loc.longitude, loc.latitude, loc.course, loc.satellites, loc.timestamp
            )
            db.add(LocationQualityLog(
                location_id=loc.id,
                device_id=device_id,
                timestamp=loc.timestamp,
                is_outlier=loc.is_outlier,
                **fields,
            ))
            created += 1
        prev1 = loc

    if write and created:
        db.commit()
    else:
        db.rollback()

    return created


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--device-id", type=int, help="Process a single device")
    group.add_argument("--all-devices", action="store_true", help="Process every device")
    parser.add_argument("--write", action="store_true", help="Persist changes (default: dry run, no writes)")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        if args.all_devices:
            device_ids = [d.id for d in db.query(Device.id).all()]
        else:
            device_ids = [args.device_id]

        mode = "WRITE" if args.write else "DRY RUN"
        total_created = 0
        for device_id in device_ids:
            created = process_device(db, device_id, write=args.write)
            total_created += created
            logger.info(f"[{mode}] device {device_id}: {created} row(s) {'created' if args.write else 'would be created'}")

        logger.info(f"\n[{mode}] Total: {total_created} row(s) {'created' if args.write else 'would be created'} "
                    f"across {len(device_ids)} device(s).")
        if not args.write and total_created:
            logger.info("Re-run with --write to persist these changes.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
