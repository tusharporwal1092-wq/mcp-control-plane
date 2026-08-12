"""Daily audit-log export to S3 (docs/roadmap.md Phase 5 "S3 export").

Streams the previous UTC day's audit_log rows as NDJSON (same row shape as
`GET /admin/audit/export` in app/main.py) and uploads them to S3 under
`audit-log/{date}.ndjson`. Per the retention policy in docs/architecture.md
S3.5: 90 days live in Postgres, this export is what feeds the 7-year S3
Glacier copy - the Glacier lifecycle transition itself is an S3 bucket rule
(Terraform, Phase 7 - not built anywhere in this repo yet), not something
this script does.

Run manually:
    uv run python scripts/export_audit_to_s3.py

Or on a schedule via .github/workflows/audit-export.yml (daily cron) - that
workflow just runs this script with the right environment/secrets; it's not
a scheduler in its own right.

Requires:
    S3_BUCKET      - target bucket (no default: refuses to run without one
                      rather than silently exporting nowhere)
    DATABASE_URL   - same variable app/db.py reads (default: local Postgres)
    AWS credentials via boto3's standard chain (env vars, ~/.aws/credentials,
    or an IAM role when run in CI)

ponytail: collects the whole day's NDJSON in memory before a single
put_object call, rather than a multipart streaming upload - fine at roughly
one row per tool call per day; revisit with S3 multipart upload if daily
audit volume ever gets large enough for this to matter.
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import asyncpg
import boto3

# This script lives outside the `app` package but imports from it - put the
# repo root on sys.path so `from app.audit import ...` resolves when run
# directly (`python scripts/export_audit_to_s3.py`), same as tests/ does.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.audit import export_audit_log  # noqa: E402
from app.db import DATABASE_URL  # noqa: E402


def _prior_utc_day() -> tuple[datetime, datetime]:
    """[start, end) for "yesterday" in UTC, both at midnight. Called once
    from main() to pick the export window - "yesterday" rather than "today"
    since today isn't over yet and a partial day's export would need
    re-running anyway."""
    today_midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return today_midnight - timedelta(days=1), today_midnight


async def _collect_ndjson(pool: asyncpg.Pool, from_: datetime, to: datetime) -> str:
    """Drain app.audit.export_audit_log's row-by-row generator into one
    newline-delimited string, ready for a single S3 put_object call."""
    lines = [json.dumps(row) async for row in export_audit_log(pool, from_=from_, to=to)]
    return "\n".join(lines) + ("\n" if lines else "")


async def main() -> None:
    bucket = os.environ["S3_BUCKET"]  # KeyError with a clear name beats a silent no-op export
    from_, to = _prior_utc_day()

    # Short-lived pool: this process runs once (via cron) and exits, so a
    # small pool just for this one query is enough - no benefit to reusing
    # app/db.py's create_db_pool() sizing, which is tuned for a long-running
    # server handling many concurrent requests.
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=2)
    try:
        body = await _collect_ndjson(pool, from_, to)
    finally:
        await pool.close()

    if not body:
        # A quiet day (no tool calls) isn't an error - just nothing to upload.
        print(f"no audit_log rows for {from_.date().isoformat()} - skipping upload")
        return

    key = f"audit-log/{from_.date().isoformat()}.ndjson"
    boto3.client("s3").put_object(Bucket=bucket, Key=key, Body=body.encode(), ContentType="application/x-ndjson")
    print(f"exported {body.count(chr(10))} row(s) to s3://{bucket}/{key}")


if __name__ == "__main__":
    asyncio.run(main())
