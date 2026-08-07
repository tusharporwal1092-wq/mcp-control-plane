"""End-to-end integration tests against a *real* OPA server (Docker) loaded
with the actual policies/ directory - unlike test_opa.py (mocked HTTP) and
test_mcp_integration.py (stubbed evaluate_policy), this exercises the real
Rego rules through the real gateway. Skipped automatically when Docker isn't
available (e.g. plain `uv run pytest` on a machine without it).
"""
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import main as app_main  # noqa: E402
from app.authz import opa as opa_module  # noqa: E402
from fakes import FakeRedis  # noqa: E402

POLICIES_DIR = Path(__file__).resolve().parent.parent / "policies"


def _docker_available() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


pytestmark = pytest.mark.skipif(
    not _docker_available(), reason="docker not available for real-OPA integration tests"
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
def opa_url():
    """Run the real openpolicyagent/opa image against policies/, the same
    way docker-compose.yaml's `opa` service does."""
    port = _free_port()
    result = subprocess.run(
        [
            "docker", "run", "-d", "--rm",
            "-p", f"{port}:8181",
            "-v", f"{POLICIES_DIR}:/policies",
            "openpolicyagent/opa:latest",
            "run", "--server", "--addr=0.0.0.0:8181", "/policies",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    container_id = result.stdout.strip()
    try:
        for _ in range(30):
            try:
                if httpx.get(f"http://localhost:{port}/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        else:
            pytest.fail("OPA container did not become ready in time")
        yield f"http://localhost:{port}/v1/data/authz"
    finally:
        subprocess.run(["docker", "stop", container_id], capture_output=True)


@pytest.fixture
def real_opa_client(monkeypatch, opa_url):
    monkeypatch.setattr(app_main, "create_redis_client", lambda: FakeRedis())
    monkeypatch.setattr(opa_module, "OPA_URL", opa_url)
    with TestClient(app_main.app) as test_client:
        yield test_client


def test_sre_get_pod_logs_allowed_in_prod(real_opa_client):
    response = real_opa_client.post(
        "/mcp",
        json=rpc("tools/call", {"name": "get_pod_logs", "arguments": {"namespace": "prod-payments"}}),
        headers={"x-api-key": "test_key"},
    )
    assert response.status_code == 200
    assert response.json()["result"] == {"status": "success"}


def test_sre_restart_deployment_in_prod_requires_approval(real_opa_client):
    response = real_opa_client.post(
        "/mcp",
        json=rpc("tools/call", {"name": "restart_deployment", "arguments": {"namespace": "prod-payments"}}),
        headers={"x-api-key": "test_key"},
    )
    assert response.status_code == 403
    body = response.json()
    assert body["error"]["code"] == -32010
    assert body["error"]["data"]["policy_decision"] == "require_approval"
