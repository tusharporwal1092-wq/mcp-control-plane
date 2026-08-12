"""Slack integration for the approval gate.

Sends the pending-approval notification via Incoming Webhook, and verifies
the `X-Slack-Signature` HMAC (plus timestamp, to reject replays) on the
interactive callback per docs/threat-model.md T-07 "Fake Approval Injection".
"""
import hashlib
import hmac
import logging
import os
import time

import httpx

from .approvals import Approval

logger = logging.getLogger(__name__)

SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "")
# Slack's own guidance for its request signing scheme: reject anything whose
# timestamp is more than 5 minutes old, so a captured (validly signed)
# request can't be replayed later to re-trigger a decision.
REPLAY_TOLERANCE_SECONDS = 300


async def send_approval_request(approval: Approval) -> None:
    """POST the pending approval to Slack as a Block Kit message with
    Approve/Deny buttons (value = approval id)."""
    if not SLACK_WEBHOOK_URL:
        logger.warning("SLACK_WEBHOOK_URL not configured; skipping Slack notification for %s", approval.id)
        return

    message = {
        "text": f"Approval required: {approval.tool_name} by {approval.agent_id}",
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*Approval required*\n"
                        f"Agent: `{approval.agent_id}` ({approval.role})\n"
                        f"Tool: `{approval.tool_name}`\n"
                        f"Arguments: `{approval.arguments}`\n"
                        f"Reason: {approval.reason}\n"
                        f"Approval ID: `{approval.id}`"
                    ),
                },
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Approve"},
                        "style": "primary",
                        "action_id": "approve",
                        "value": approval.id,
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Deny"},
                        "style": "danger",
                        "action_id": "deny",
                        "value": approval.id,
                    },
                ],
            },
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(SLACK_WEBHOOK_URL, json=message)
            response.raise_for_status()
    except httpx.HTTPError:
        logger.exception("Failed to send Slack approval notification for %s", approval.id)


def verify_signature(timestamp: str | None, body: bytes, signature: str | None) -> bool:
    """Verify Slack's `X-Slack-Signature` header over the raw request body.

    Checks the HMAC *and* the timestamp - a signature alone would still
    validate a captured request replayed well after the fact.
    """
    if not timestamp or not signature or not SLACK_SIGNING_SECRET:
        return False
    try:
        request_ts = int(timestamp)
    except ValueError:
        return False
    if abs(time.time() - request_ts) > REPLAY_TOLERANCE_SECONDS:
        return False

    basestring = b"v0:" + timestamp.encode() + b":" + body
    computed = "v0=" + hmac.new(SLACK_SIGNING_SECRET.encode(), basestring, hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed, signature)
