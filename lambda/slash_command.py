"""
Bedrock spend slash commands and interaction handler.

Slash commands:
  /bedrock-spend                         — show caller's current spend
  /bedrock-block <username> [duration]   — block a user (admin only, requires confirm)
  /bedrock-unblock <username>            — remove manual block (admin only, requires confirm)
  /bedrock-limit <username> <amount>     — set custom daily limit (admin only, requires confirm)

Interactive callbacks:
  POST /bedrock-interact — handles button confirm/cancel from block/unblock/limit prompts

The Lambda runs in two modes to meet Slack's 3-second response requirement:
  gateway      — verify HMAC, ACK immediately (empty 200), self-invoke async
  async        — do the real work, post result via response_url

CUSTOMIZE
  - SLACK_EMAIL_DOMAIN: must match notifier.py.
  - MAX_SETTABLE_LIMIT: hard ceiling on admin-settable per-user limits.
  - Message copy in _ephemeral() / _post_result() helpers is neutral — adapt as needed.
"""

import base64
import hashlib
import hmac
import json
import logging
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs

import boto3
from botocore.client import BaseClient
from botocore.config import Config
from slack_sdk import WebClient

logger = logging.getLogger()
logger.setLevel(logging.INFO)

STATE_TABLE = os.environ.get("STATE_TABLE", "bedrock-spend-enforcement-state")
EXCEPTIONS_TABLE = os.environ.get("EXCEPTIONS_TABLE", "bedrock-spend-enforcement-exceptions")
DEFAULT_DAILY_LIMIT = float(os.environ.get("DEFAULT_DAILY_LIMIT", "150"))
MAX_SETTABLE_LIMIT = float(os.environ.get("MAX_SETTABLE_LIMIT", "1000"))
SIGNATURE_MAX_AGE_SECONDS = 300
_CONTENT_TYPE_JSON = "application/json"

_BOTO_CONFIG = Config(connect_timeout=5, read_timeout=5)
_lambda_client: BaseClient = boto3.client("lambda", config=Config(connect_timeout=3, read_timeout=3))
_dynamodb_resource = boto3.resource("dynamodb", config=_BOTO_CONFIG)

_DURATION_MAP: dict[str, timedelta | None] = {
    "1h": timedelta(hours=1),
    "2h": timedelta(hours=2),
    "4h": timedelta(hours=4),
    "8h": timedelta(hours=8),
    "12h": timedelta(hours=12),
    "24h": timedelta(hours=24),
    "1d": timedelta(days=1),
    "2d": timedelta(days=2),
    "3d": timedelta(days=3),
    "7d": timedelta(days=7),
    "1w": timedelta(weeks=1),
    "2w": timedelta(weeks=2),
}
_VALID_USERNAME = re.compile(r"^[a-z]+(?:-[a-z]+)*\.[a-z]+(?:-[a-z]+)*$")
_VALID_DURATIONS = ", ".join(
    sorted(_DURATION_MAP.keys(), key=lambda k: _DURATION_MAP[k] or timedelta.max)
)


# ── Auth ──────────────────────────────────────────────────────────────────────

def _is_admin(caller_slack_id: str) -> bool:
    admin_ids = {uid.strip() for uid in os.environ.get("ADMIN_SLACK_IDS", "").split(",") if uid.strip()}
    return caller_slack_id in admin_ids


# ── Signature verification ────────────────────────────────────────────────────

def _verify_slack_signature(signature: str, timestamp: str, body: str) -> bool:
    """Verify Slack HMAC-SHA256 request signature. Rejects stale requests."""
    secret = os.environ.get("SLACK_SIGNING_SECRET", "")
    if not secret:
        logger.error("SLACK_SIGNING_SECRET is not configured")
        return False
    try:
        stale = abs(int(time.time()) - int(timestamp)) > SIGNATURE_MAX_AGE_SECONDS
        expected = "v0=" + hmac.new(
            secret.encode(),
            f"v0:{timestamp}:{body}".encode(),
            hashlib.sha256,
        ).hexdigest()
        return not stale and hmac.compare_digest(expected, signature)
    except (ValueError, TypeError):
        logger.exception("Error verifying Slack signature")
        return False


# ── Slack helpers ─────────────────────────────────────────────────────────────

def _ack() -> dict[str, Any]:
    return {"statusCode": 200, "body": ""}


def _ephemeral(text: str) -> dict[str, Any]:
    return {
        "statusCode": 200,
        "headers": {"Content-Type": _CONTENT_TYPE_JSON},
        "body": json.dumps({"response_type": "ephemeral", "text": text}),
    }


def _post_response_url(response_url: str, text: str) -> None:
    import urllib.request
    payload = json.dumps(
        {"response_type": "ephemeral", "replace_original": False, "text": text}
    ).encode()
    req = urllib.request.Request(
        response_url, data=payload, headers={"Content-Type": _CONTENT_TYPE_JSON}
    )
    try:
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        logger.exception("Failed to post to response_url")


def _post_ephemeral_api(channel_id: str, user_id: str, text: str) -> None:
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        logger.error("SLACK_BOT_TOKEN not configured — ephemeral delivery skipped")
        return
    try:
        WebClient(token=token).chat_postEphemeral(channel=channel_id, user=user_id, text=text)
    except Exception:
        logger.exception("Failed to post ephemeral via API channel=%s user=%s", channel_id, user_id)


def _post_result(text: str, response_url: str, channel_id: str, user_id: str) -> None:
    if channel_id and user_id:
        _post_ephemeral_api(channel_id, user_id, text)
    elif response_url:
        _post_response_url(response_url, text)
    else:
        logger.warning("No channel_id or response_url — result not delivered: %s", text)


def _resolve_username(caller_slack_id: str) -> str | None:
    """Resolve Slack user ID → SSO username via profile email."""
    try:
        client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
        info = client.users_info(user=caller_slack_id)
        email: str = info["user"]["profile"]["email"]
        return email.split("@")[0]
    except Exception:
        logger.exception("Could not resolve username for %s", caller_slack_id)
        return None


# ── DynamoDB helpers ──────────────────────────────────────────────────────────

def _current_window_start() -> datetime:
    now = datetime.now(timezone.utc)
    reset = now.replace(hour=4, minute=0, second=0, microsecond=0)
    return reset if now >= reset else reset - timedelta(days=1)


def _is_stale(updated_at: str) -> bool:
    try:
        return datetime.fromisoformat(updated_at) < _current_window_start()
    except (ValueError, TypeError):
        return False


def _get_spend(username: str) -> float:
    try:
        table = _dynamodb_resource.Table(STATE_TABLE)
        response = table.get_item(Key={"user_id": username}, ProjectionExpression="spend_usd, updated_at")
        item = response.get("Item")
        valid = item and "spend_usd" in item and not _is_stale(item.get("updated_at", ""))
        return float(item["spend_usd"]) if valid else 0.0
    except Exception:
        logger.exception("Failed to read spend for %s", username)
    return 0.0


def _get_limit(username: str) -> float:
    try:
        table = _dynamodb_resource.Table(EXCEPTIONS_TABLE)
        response = table.get_item(Key={"user_id": username})
        item = response.get("Item")
        if item and "daily_limit_usd" in item:
            return float(item["daily_limit_usd"])
    except Exception:
        logger.exception("Failed to read limit for %s", username)
    return DEFAULT_DAILY_LIMIT


def _apply_block(username: str, until_iso: str | None) -> None:
    table = _dynamodb_resource.Table(EXCEPTIONS_TABLE)
    update_expr = "SET manual_block = :t"
    expr_values: dict[str, Any] = {":t": True}
    if until_iso:
        update_expr += ", blocked_until = :u"
        expr_values[":u"] = until_iso
    else:
        update_expr += " REMOVE blocked_until"
    table.update_item(
        Key={"user_id": username},
        UpdateExpression=update_expr,
        ExpressionAttributeValues=expr_values,
    )


def _remove_block(username: str) -> None:
    table = _dynamodb_resource.Table(EXCEPTIONS_TABLE)
    table.update_item(
        Key={"user_id": username},
        UpdateExpression="REMOVE manual_block, blocked_until",
    )


def _set_limit(username: str, limit: float, granted_by: str) -> None:
    """Set a custom daily limit. Records audit trail (who granted it and when)."""
    table = _dynamodb_resource.Table(EXCEPTIONS_TABLE)
    table.update_item(
        Key={"user_id": username},
        UpdateExpression="SET daily_limit_usd = :v, granted_by = :by, granted_at = :at",
        ExpressionAttributeValues={
            ":v": str(limit),
            ":by": granted_by,
            ":at": datetime.now(timezone.utc).isoformat(),
        },
    )


def _invoke_enforcement_lambda() -> None:
    """Fire the enforcement Lambda async so CMPs are rewritten immediately after an admin action."""
    enforcement_fn = os.environ.get("ENFORCEMENT_FUNCTION_NAME", "bedrock-spend-enforcement")
    try:
        _lambda_client.invoke(
            FunctionName=enforcement_fn,
            InvocationType="Event",
            Payload=b"{}",
        )
    except Exception:
        logger.exception("Failed to invoke enforcement Lambda")


# ── Confirmation UI ───────────────────────────────────────────────────────────

def _confirm_blocks(action_type: str, description: str, action_payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": description}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Yes, do it"},
                    "style": "danger",
                    "action_id": "confirm_action",
                    "value": json.dumps({"action": action_type, **action_payload}),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Cancel"},
                    "action_id": "cancel_action",
                    "value": "cancel",
                },
            ],
        },
    ]


# ── Duration parsing ──────────────────────────────────────────────────────────

def _parse_duration(raw: str) -> tuple[timedelta | None, str] | None:
    if not raw:
        return None, "indefinitely"
    td = _DURATION_MAP.get(raw.lower())
    if td is None:
        return None  # type: ignore[return-value]
    total_hours = int(td.total_seconds() // 3600)
    if total_hours % 24 == 0:
        days = total_hours // 24
        label = f"{days} day{'s' if days != 1 else ''}"
    else:
        label = f"{total_hours} hour{'s' if total_hours != 1 else ''}"
    return td, label


# ── Entry point ───────────────────────────────────────────────────────────────

def lambda_handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    mode = event.get("mode")
    handlers = {
        "async": _handle_async,
        "interact_async": _handle_interact_async,
    }
    if mode in handlers:
        return handlers[mode](event)
    if event.get("interact"):
        return _handle_interact_gateway(event)
    return _handle_gateway(event)


# ── Slash command gateway ─────────────────────────────────────────────────────

def _handle_gateway(event: dict[str, Any]) -> dict[str, Any]:
    raw_body = event.get("body", "") or ""
    if event.get("isBase64Encoded"):
        raw_body = base64.b64decode(raw_body).decode("utf-8")
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    if not _verify_slack_signature(
        headers.get("x-slack-signature", ""),
        headers.get("x-slack-request-timestamp", ""),
        raw_body,
    ):
        logger.warning("Invalid Slack signature")
        return {"statusCode": 403, "body": ""}
    params = {k: v[0] for k, v in parse_qs(raw_body).items()}
    try:
        _lambda_client.invoke(
            FunctionName=os.environ.get("AWS_LAMBDA_FUNCTION_NAME", ""),
            InvocationType="Event",
            Payload=json.dumps({
                "mode": "async",
                "command": params.get("command", ""),
                "text": params.get("text", "").strip(),
                "caller_slack_id": params.get("user_id", ""),
                "channel_id": params.get("channel_id", ""),
                "response_url": params.get("response_url", ""),
            }).encode(),
        )
        return _ack()
    except Exception:
        logger.exception("Failed to self-invoke async")
        return _ephemeral("Something went wrong. Please try again.")


def _handle_async(event: dict[str, Any]) -> dict[str, Any]:
    try:
        command = event["command"]
        text = event.get("text", "")
        caller_slack_id = event["caller_slack_id"]
        channel_id = event.get("channel_id", "")
        response_url = event["response_url"]
    except KeyError:
        logger.exception("Malformed async payload")
        return {"statusCode": 200}
    try:
        if command == "/bedrock-spend":
            _async_spend(caller_slack_id, channel_id, response_url)
        elif command in ("/bedrock-block", "/bedrock-unblock", "/bedrock-limit"):
            _async_admin_command(command, text, caller_slack_id, channel_id, response_url)
        else:
            _post_result(f"Unknown command: {command}", response_url, channel_id, caller_slack_id)
    except Exception:
        logger.exception("Error in async handler for %s", command)
        _post_result("Something went wrong. Please try again.", response_url, channel_id, caller_slack_id)
    return {"statusCode": 200}


def _async_spend(caller_slack_id: str, channel_id: str, response_url: str) -> None:
    from notifier import slash_command_text
    username = _resolve_username(caller_slack_id)
    if not username:
        _post_result("Unable to identify your user account.", response_url, channel_id, caller_slack_id)
        return
    spend = _get_spend(username)
    limit = _get_limit(username)
    _post_result(slash_command_text(spend, limit), response_url, channel_id, caller_slack_id)


def _async_admin_command(
    command: str, text: str, caller_slack_id: str, channel_id: str, response_url: str
) -> None:
    if not _is_admin(caller_slack_id):
        _post_response_url(response_url, "That command is restricted to administrators.")
        return
    blocks = _parse_admin_command(command, text, response_url)
    if blocks is None:
        return
    client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    try:
        client.chat_postEphemeral(channel=channel_id, user=caller_slack_id, blocks=blocks, text="Confirmation required")
    except Exception:
        logger.exception("Failed to post confirmation prompt")
        _post_response_url(response_url, "Failed to show confirmation. Please try again.")


def _parse_admin_command(command: str, text: str, response_url: str) -> list[dict[str, Any]] | None:
    parts = text.split()
    if command == "/bedrock-block":
        return _parse_block_command(parts, response_url)
    if command == "/bedrock-unblock":
        return _parse_unblock_command(parts, response_url)
    return _parse_limit_command(parts, response_url)


def _parse_block_command(parts: list[str], response_url: str) -> list[dict[str, Any]] | None:
    if not parts:
        _post_response_url(response_url, f"Usage: `/bedrock-block <username> [duration]`\nValid durations: {_VALID_DURATIONS}")
        return None
    username = parts[0]
    raw_duration = parts[1] if len(parts) > 1 else ""
    parsed = _parse_duration(raw_duration)
    if parsed is None:
        _post_response_url(response_url, f"Unknown duration `{raw_duration}`. Valid durations: {_VALID_DURATIONS}")
        return None
    td, label = parsed
    until_iso: str | None = (datetime.now(timezone.utc) + td).isoformat() if td else None
    description = f"Block *{username}* from Bedrock for *{label}*?"
    if until_iso:
        description += f"\nAccess restored after: {until_iso[:16]} UTC"
    return _confirm_blocks("block", description, {"username": username, "until_iso": until_iso, "label": label})


def _parse_unblock_command(parts: list[str], response_url: str) -> list[dict[str, Any]] | None:
    if not parts:
        _post_response_url(response_url, "Usage: `/bedrock-unblock <username>`")
        return None
    return _confirm_blocks("unblock", f"Remove the block on *{parts[0]}*?", {"username": parts[0]})


def _limit_amount_error(amount: float) -> str | None:
    if not math.isfinite(amount) or amount <= 0:
        return f"Invalid amount: `{amount}`. Please provide a positive number."
    if amount > MAX_SETTABLE_LIMIT:
        return f"${amount:.0f} exceeds the maximum settable daily limit of ${MAX_SETTABLE_LIMIT:.0f}."
    return None


def _validate_limit_amount(raw: str) -> tuple[float | None, str | None]:
    try:
        amount = float(raw)
    except ValueError:
        return None, f"Invalid amount: `{raw}`. Please provide a positive number."
    error = _limit_amount_error(amount)
    return (None, error) if error else (amount, None)


def _parse_limit_command(parts: list[str], response_url: str) -> list[dict[str, Any]] | None:
    if len(parts) < 2:
        _post_response_url(response_url, "Usage: `/bedrock-limit <username> <amount>`\nExample: `/bedrock-limit jane.doe 300`")
        return None
    username = parts[0]
    amount, error = _validate_limit_amount(parts[1])
    if error:
        _post_response_url(response_url, error)
        return None
    direction = "raise" if amount > DEFAULT_DAILY_LIMIT else "lower"
    description = f"Set *{username}*'s daily limit to *${amount:.0f}* (default is ${DEFAULT_DAILY_LIMIT:.0f})?\nThis will *{direction}* their limit."
    return _confirm_blocks("limit", description, {"username": username, "amount": amount})


# ── Interaction gateway (button callbacks) ────────────────────────────────────

def _handle_interact_gateway(event: dict[str, Any]) -> dict[str, Any]:
    raw_body = event.get("body", "") or ""
    if event.get("isBase64Encoded"):
        raw_body = base64.b64decode(raw_body).decode("utf-8")
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    if not _verify_slack_signature(
        headers.get("x-slack-signature", ""),
        headers.get("x-slack-request-timestamp", ""),
        raw_body,
    ):
        return {"statusCode": 403, "body": ""}
    params = parse_qs(raw_body)
    payload_raw = params.get("payload", [""])[0]
    try:
        payload = json.loads(payload_raw)
    except Exception:
        return {"statusCode": 400, "body": ""}
    actions = payload.get("actions", [])
    if not actions:
        return _ack()
    action = actions[0]
    try:
        _lambda_client.invoke(
            FunctionName=os.environ.get("AWS_LAMBDA_FUNCTION_NAME", ""),
            InvocationType="Event",
            Payload=json.dumps({
                "mode": "interact_async",
                "action_id": action.get("action_id"),
                "value": action.get("value", ""),
                "caller_slack_id": payload.get("user", {}).get("id", ""),
                "channel_id": payload.get("channel", {}).get("id", ""),
                "response_url": payload.get("response_url", ""),
            }).encode(),
        )
        return _ack()
    except Exception:
        logger.exception("Failed to self-invoke interact async")
        return _ephemeral("Something went wrong. Please try again.")


def _handle_interact_async(event: dict[str, Any]) -> dict[str, Any]:
    try:
        action_id = event["action_id"]
        value = event["value"]
        caller_slack_id = event["caller_slack_id"]
        channel_id = event.get("channel_id", "")
        response_url = event.get("response_url", "")
    except KeyError:
        logger.exception("Malformed interact_async payload")
        return {"statusCode": 200}
    if action_id == "cancel_action" or value == "cancel":
        return {"statusCode": 200}
    if not _is_admin(caller_slack_id):
        _post_result("That action is restricted to administrators.", response_url, channel_id, caller_slack_id)
        return {"statusCode": 200}
    try:
        data = json.loads(value)
    except Exception:
        _post_result("Something went wrong. Please try again.", response_url, channel_id, caller_slack_id)
        return {"statusCode": 200}
    action = data.get("action")
    username = data.get("username", "")
    if not _VALID_USERNAME.match(username):
        _post_result(f"Invalid username: `{username}`. Action rejected.", response_url, channel_id, caller_slack_id)
        return {"statusCode": 200}
    try:
        _execute_action(action, username, data, response_url, channel_id, caller_slack_id)
    except Exception:
        logger.exception("Error executing action %s for %s", action, username)
        _post_result("Something went wrong executing that action. Please try again.", response_url, channel_id, caller_slack_id)
    return {"statusCode": 200}


def _execute_action(
    action: str | None, username: str, data: dict[str, Any],
    response_url: str, channel_id: str, caller_slack_id: str
) -> None:
    if action == "block":
        until_iso = data.get("until_iso")
        label = data.get("label", "indefinitely")
        _apply_block(username, until_iso)
        _invoke_enforcement_lambda()
        _post_result(f"*{username}* has been blocked from Bedrock for *{label}*. CMPs will update within 15 minutes.", response_url, channel_id, caller_slack_id)
    elif action == "unblock":
        _remove_block(username)
        _invoke_enforcement_lambda()
        _post_result(f"The block on *{username}* has been lifted. Normal spend-based enforcement resumes on the next run.", response_url, channel_id, caller_slack_id)
    elif action == "limit":
        amount = float(data["amount"])
        amount_error = _limit_amount_error(amount)
        if amount_error:
            _post_result(amount_error, response_url, channel_id, caller_slack_id)
            return
        _set_limit(username, amount, caller_slack_id)
        _post_result(f"*{username}*'s daily limit has been set to *${amount:.0f}*. Takes effect on the next run.", response_url, channel_id, caller_slack_id)
    else:
        _post_result(f"Unknown action: `{action}`", response_url, channel_id, caller_slack_id)
