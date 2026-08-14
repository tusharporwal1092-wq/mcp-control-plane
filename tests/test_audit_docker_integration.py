"""End-to-end integration tests against a *real* Postgres server (Docker):
runs the actual `alembic upgrade head` migration, then exercises
app/audit.py's hash-chain writer, `GET /admin/audit`'s integrity_check, and
`GET /admin/audit/export` - unlike the rest of the suite, which stubs
`record_tool_call` to a no-op (tests/conftest.py's `client` fixture) since
audit persistence needs a real database to mean anything. Skipped
automatically when Docker isn't available, same pattern as
test_opa_docker_integration.py.
"""
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import audit as audit_module  # noqa: E402
from app import main as app_main  # noqa: E402
from app.audit import AuditEvent  # noqa: E402
from app.authz.opa import PolicyDecision  # noqa: E402
from fakes import FakeRedis  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


def _docker_available() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


pytestmark = pytest.mark.skipif(
    not _docker_available(), reason="docker not available for real-Postgres integration tests"
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def rpc(method, params=None, id_=1):
    body = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        body["params"] = params
    return body


@pytest.fixture(scope="module")
def database_url():
    """Run a real postgres:16-alpine container (same image docker-compose.yaml
    uses) and apply the actual Alembic migration against it - `uv run alembic
    upgrade head`, the same command a real deploy would run, not a
    hand-copied CREATE TABLE that could drift from migrations/versions/."""
    # Step 1: start the container, on a free host port so this can run
    # alongside other things (including another instance of this same test
    # file) without a fixed-port collision.
    port = _free_port()
    result = subprocess.run(
        [
            "docker", "run", "-d", "--rm",
            "-p", f"{port}:5432",
            "-e", "POSTGRES_USER=postgres",
            "-e", "POSTGRES_PASSWORD=postgres",
            "-e", "POSTGRES_DB=mcp_control_plane_test",
            "postgres:16-alpine",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    container_id = result.stdout.strip()
    url = f"postgresql://postgres:postgres@localhost:{port}/mcp_control_plane_test"
    try:
        # Step 2: poll until Postgres is actually accepting connections -
        # `docker run -d` returns as soon as the container starts, well
        # before the database inside it has finished initializing.
        for _ in range(60):
            ready = subprocess.run(["docker", "exec", container_id, "pg_isready", "-U", "postgres"], capture_output=True)
            if ready.returncode == 0:
                break
            time.sleep(0.5)
        else:
            pytest.fail("Postgres container did not become ready in time")

        # Step 3: apply the schema via the real migration command, pointed
        # at this throwaway container via DATABASE_URL.
        migrate = subprocess.run(
            ["uv", "run", "alembic", "upgrade", "head"],
            cwd=REPO_ROOT,
            env={**os.environ, "DATABASE_URL": url},
            capture_output=True,
            text=True,
        )
        if migrate.returncode != 0:
            pytest.fail(f"alembic upgrade head failed:\nstdout: {migrate.stdout}\nstderr: {migrate.stderr}")

        # Hand the ready, migrated database's URL to every test in this
        # module; teardown (stopping the container) runs once, after the
        # last test that needs it finishes.
        yield url
    finally:
        subprocess.run(["docker", "stop", container_id], capture_output=True)


@pytest.fixture
async def db_pool(database_url):
    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=5)
    yield pool
    # Clear rows between tests so each test's row_hash chain starts fresh
    # from GENESIS_HASH, rather than every test in this module sharing one
    # long chain (and the schema itself, which stays - re-migrating per test
    # would be much slower for no benefit).
    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE audit_log RESTART IDENTITY")
    await pool.close()


@pytest.fixture
def real_audit_client(monkeypatch, database_url):
    """TestClient's ASGI app runs on its own event loop (a separate thread's
    portal), not the pytest-asyncio loop the `db_pool` fixture's pool is
    bound to - asyncpg pools can't cross loops. So this builds the app its
    *own* pool (against the same real database_url) rather than reusing
    `db_pool`; tests that need out-of-band assertions take `db_pool` too,
    as an independent connection to the same Postgres instance."""
    monkeypatch.setattr(app_main, "create_redis_client", lambda: FakeRedis())

    async def _real_db_pool():
        return await asyncpg.create_pool(database_url, min_size=1, max_size=5)

    monkeypatch.setattr(app_main, "create_db_pool", _real_db_pool)

    async def _allow_all(context):
        return PolicyDecision(allow=True, require_approval=False, reason="test stub: allow")

    monkeypatch.setattr(app_main, "evaluate_policy", _allow_all)
    monkeypatch.setitem(app_main.TOOLS, "list_pods", lambda arguments: {"pods": []})
    monkeypatch.setitem(app_main.TOOLS, "get_pod_logs", lambda arguments: {"logs": "ok"})
    with TestClient(app_main.app) as test_client:
        yield test_client


async def test_every_tool_call_gets_an_audit_row(real_audit_client, db_pool):
    """Exit criterion: N tool calls through the real HTTP path -> N rows."""
    for i in range(3):
        response = real_audit_client.post(
            "/mcp",
            json=rpc("tools/call", {"name": "list_pods", "arguments": {"namespace": "payments"}}, id_=i),
            headers={"x-api-key": "test_key"},
        )
        assert response.status_code == 200

    async with db_pool.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM audit_log WHERE tool_name = 'list_pods'")
        trace_ids = await conn.fetch("SELECT otel_trace_id FROM audit_log WHERE tool_name = 'list_pods'")
    assert count == 3
    # Phase 6: every row written inside a real request carries the request's
    # trace id (app/audit.py::_current_trace_id) - a real span exists (and
    # gets a real random trace id) whether or not an OTLP collector is
    # actually listening, so this doesn't need OTEL_EXPORTER_OTLP_ENDPOINT set.
    for row in trace_ids:
        assert row["otel_trace_id"] is not None
        assert len(row["otel_trace_id"]) == 32
        int(row["otel_trace_id"], 16)  # valid hex


async def test_hash_chain_tamper_is_detected(db_pool):
    """Write rows, verify the chain passes, tamper with one row directly via
    SQL (the same thing an insider with raw DB access could do -
    docs/threat-model.md T-04), and confirm integrity_check flips to "fail"
    at exactly that row's seq."""
    for i in range(3):
        await audit_module.record_tool_call(
            db_pool,
            AuditEvent(
                agent_id="agent01",
                role="sre1",
                tool_name="list_pods",
                arguments={"namespace": "payments", "n": i},
                decision="allowed",
                reason="test",
                result={"ok": True},
            ),
        )

    # Untouched chain: should verify clean.
    total, rows, status, broken_seq = await audit_module.query_audit_log(db_pool, limit=10)
    assert total == 3
    assert status == "pass"
    assert broken_seq is None

    # Tamper with the middle row's args directly via SQL - row_hash itself
    # is left untouched, so this simulates an edit that didn't bother
    # recomputing the hash (exactly what a real tamper attempt would do).
    middle_seq = sorted(r["seq"] for r in rows)[1]
    async with db_pool.acquire() as conn:
        await conn.execute("""UPDATE audit_log SET args = '{"tampered": true}'::jsonb WHERE seq = $1""", middle_seq)

    # Re-verify: the stored row_hash no longer matches what the (now
    # different) args actually hash to, so integrity_check must flip to
    # "fail" and pinpoint the tampered row's own seq.
    total, rows, status, broken_seq = await audit_module.query_audit_log(db_pool, limit=10)
    assert status == "fail"
    assert broken_seq == middle_seq


async def test_query_endpoint_filters_and_reports_integrity(real_audit_client, db_pool):
    # `db_pool` isn't used directly here, but requesting it means its
    # fixture teardown truncates audit_log after this test - without it,
    # this test's row leaks into whichever test runs next (see the
    # `db_pool`/`real_audit_client` fixtures above).
    real_audit_client.post(
        "/mcp",
        json=rpc("tools/call", {"name": "list_pods", "arguments": {"namespace": "payments"}}),
        headers={"x-api-key": "test_key"},
    )

    response = real_audit_client.get("/admin/audit", params={"tool": "list_pods", "limit": 10})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["integrity_check"] == "pass"
    assert body["rows"][0]["tool_name"] == "list_pods"
    assert body["rows"][0]["result_status"] == "allowed"


async def test_export_endpoint_returns_well_formed_ndjson(real_audit_client, db_pool):
    real_audit_client.post(
        "/mcp",
        json=rpc("tools/call", {"name": "get_pod_logs", "arguments": {"namespace": "payments", "pod_name": "x"}}),
        headers={"x-api-key": "test_key"},
    )

    from_ = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    to = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    # params=, not an f-string into the URL: httpx percent-encodes "+" (the
    # UTC offset in an ISO timestamp) here, where a raw query string would
    # let it decode as a space and corrupt the timestamp.
    response = real_audit_client.get("/admin/audit/export", params={"from": from_, "to": to})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")

    lines = [line for line in response.text.strip().split("\n") if line]
    assert len(lines) >= 1
    required_fields = {
        "id", "seq", "agent_id", "role", "tool_name", "args", "policy_decision",
        "approval_id", "result_status", "result_summary", "duration_ms",
        "otel_trace_id", "row_hash", "created_at",
    }
    for line in lines:
        row = json.loads(line)
        assert required_fields <= row.keys()
        assert row["tool_name"] == "get_pod_logs"
        assert row["result_status"] == "allowed"
