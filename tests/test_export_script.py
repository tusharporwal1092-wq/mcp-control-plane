"""Smoke test for scripts/export_audit_to_s3.py's date math - the one bit of
non-trivial logic in that script that doesn't need a real Postgres/S3 to
verify (the DB-reading half is exercised by test_audit_docker_integration.py
via the same app.audit.export_audit_log it calls).
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from export_audit_to_s3 import _prior_utc_day  # noqa: E402


def test_prior_utc_day_is_midnight_to_midnight_yesterday():
    from_, to = _prior_utc_day()

    now = datetime.now(timezone.utc)
    assert from_.tzinfo is not None
    assert to - from_ == timedelta(days=1)
    assert from_.date() == (now - timedelta(days=1)).date()
    assert to.time() == datetime.min.time()
    assert from_.time() == datetime.min.time()
    assert to <= now
