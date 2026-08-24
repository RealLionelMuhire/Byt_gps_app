"""
Retroactively flag existing `locations` rows as is_outlier using the same
3-point-window plausibility check applied to new fixes at ingestion time
(classify_outlier in app/api/locations.py — imported here, not
reimplemented, so live ingestion and this backfill can never disagree).

Never deletes or overwrites lat/lon/timestamp — only sets is_outlier.

Defaults to DRY RUN (reports what would change, writes nothing). Pass
--write to actually persist changes.

Default mode only ADDS flags (never unflags an already-flagged row), so it's
always safe to re-run after new points arrive. Pass --reset to instead do a
full clean recompute: every row's is_outlier is cleared first, then
recomputed from scratch under the CURRENT classify_outlier code. Use --reset
when the algorithm has changed since the last write pass and old rows may
carry stale flags from an earlier version that the additive-only mode can't
clear (its unflag report shows exactly which rows that affects, so verify
the dry-run output before writing).

Usage:
  # Dry run for one device:
  python scripts/backfill_location_outliers.py --device-id 1

  # Dry run for every device:
  python scripts/backfill_location_outliers.py --all-devices

  # Real write pass for one device:
  python scripts/backfill_location_outliers.py --device-id 1 --write

  # Full clean recompute (clears stale flags from an earlier algorithm
  # version), dry run first:
  python scripts/backfill_location_outliers.py --device-id 1 --reset
  python scripts/backfill_location_outliers.py --device-id 1 --reset --write
"""

import argparse
import sys
import os
import logging

# Match the pattern used by cron_expiry.py / migrate_add_expo_push_token.py
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.core.database import SessionLocal
from app.models.location import Location
from app.models.location_quality_log import LocationQualityLog
from app.models.device import Device
from app.api.locations import classify_outlier

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)


def process_device(db, device_id: int, write: bool, reset: bool) -> list[dict]:
    """
    Walk one device's location history chronologically, applying
    classify_outlier over a rolling 3-point window. Returns a list of
    change records (empty if nothing would change).

    Default (reset=False): only ADDS flags — a row whose is_outlier is
    currently False and would become True. Never unflags an already-flagged
    row, so it's safe to re-run after new points arrive without needing to
    know the full history again.

    reset=True: clears every row's is_outlier first, then recomputes from
    scratch — also reports rows whose flag is REMOVED (stale flag from an
    earlier algorithm version that additive-only mode can't clear). Also
    syncs the matching location_quality_log.is_outlier for every changed row.
    """
    locations = (
        db.query(Location)
        .filter(Location.device_id == device_id)
        .order_by(Location.timestamp.asc())
        .all()
    )

    if reset:
        starting_flagged_ids = {loc.id for loc in locations if loc.is_outlier}
        for loc in locations:
            loc.is_outlier = False
    else:
        starting_flagged_ids = None

    changes = []
    prev2 = None
    prev1 = None
    for loc in locations:
        if prev1 is not None:
            new_is_outlier, flag_prev1 = classify_outlier(
                prev2, prev1, loc.longitude, loc.latitude, loc.timestamp
            )
            # Mutate in-memory regardless of dry-run, so later iterations in
            # THIS walk see the same is_outlier state a --write run would
            # have produced (otherwise a point can be mis-reported twice,
            # once forward-flagged then again retroactively, when write
            # mode would have suppressed the second check). Only db.commit()
            # is gated on `write` — nothing reaches the DB in dry-run mode.
            if flag_prev1 and not prev1.is_outlier:
                changes.append({
                    "location_id": prev1.id,
                    "timestamp": prev1.timestamp,
                    "latitude": prev1.latitude,
                    "longitude": prev1.longitude,
                    "reason": "retroactive: skip-leg plausible (jump-and-return spike)",
                })
                prev1.is_outlier = True
            if new_is_outlier and not loc.is_outlier:
                changes.append({
                    "location_id": loc.id,
                    "timestamp": loc.timestamp,
                    "latitude": loc.latitude,
                    "longitude": loc.longitude,
                    "reason": "forward leg implausible",
                })
                loc.is_outlier = True
        prev2 = prev1
        prev1 = loc

    if reset:
        final_flagged_ids = {loc.id for loc in locations if loc.is_outlier}
        removed_ids = starting_flagged_ids - final_flagged_ids
        if removed_ids:
            by_id = {loc.id: loc for loc in locations}
            for lid in sorted(removed_ids):
                loc = by_id[lid]
                changes.append({
                    "location_id": loc.id,
                    "timestamp": loc.timestamp,
                    "latitude": loc.latitude,
                    "longitude": loc.longitude,
                    "reason": "UNFLAGGED: stale from an earlier algorithm version, current code doesn't flag it",
                })

    if write and changes:
        # Keep location_quality_log.is_outlier in sync with the rows that changed.
        changed_ids = [c["location_id"] for c in changes]
        by_id = {loc.id: loc for loc in locations}
        logs = (
            db.query(LocationQualityLog)
            .filter(LocationQualityLog.location_id.in_(changed_ids))
            .all()
        )
        for log_row in logs:
            log_row.is_outlier = by_id[log_row.location_id].is_outlier
        db.commit()
    else:
        db.rollback()  # discard in-memory mutations made for accurate dry-run reporting

    return changes


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--device-id", type=int, help="Process a single device")
    group.add_argument("--all-devices", action="store_true", help="Process every device")
    parser.add_argument("--write", action="store_true", help="Persist changes (default: dry run, no writes)")
    parser.add_argument(
        "--reset", action="store_true",
        help="Clear every row's is_outlier first, then recompute from scratch under the current "
             "algorithm (also reports/clears stale flags left by an earlier algorithm version). "
             "Default mode only adds flags and never unflags anything."
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        if args.all_devices:
            device_ids = [d.id for d in db.query(Device.id).all()]
        else:
            device_ids = [args.device_id]

        mode = "WRITE" if args.write else "DRY RUN"
        if args.reset:
            mode += "+RESET"
        total_changes = 0
        total_unflagged = 0
        for device_id in device_ids:
            changes = process_device(db, device_id, write=args.write, reset=args.reset)
            total_changes += len(changes)
            total_unflagged += sum(1 for c in changes if c["reason"].startswith("UNFLAGGED"))
            if not changes:
                logger.info(f"[{mode}] device {device_id}: no changes")
                continue
            logger.info(f"[{mode}] device {device_id}: {len(changes)} change(s)")
            for c in changes:
                logger.info(
                    f"    id={c['location_id']} ts={c['timestamp']} "
                    f"lat={c['latitude']:.6f} lon={c['longitude']:.6f} — {c['reason']}"
                )

        added = total_changes - total_unflagged
        logger.info(f"\n[{mode}] Total: {added} point(s) {'flagged' if args.write else 'would be flagged'}, "
                    f"{total_unflagged} point(s) {'unflagged' if args.write else 'would be unflagged'} "
                    f"across {len(device_ids)} device(s).")
        if not args.write and total_changes:
            logger.info("Re-run with --write (keep --reset if used here) to persist these changes.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
