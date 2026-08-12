"""
Bedrock spend notifications.

Sends one Slack DM per tier transition (warn / t1 / t2). Resolves SSO
username to Slack user ID via users.lookupByEmail.

CUSTOMIZE
  - SLACK_EMAIL_DOMAIN: set to your org's email domain so that SSO username
    "jane.doe" resolves to "jane.doe@<your-domain>".
  - Message text: the _*_text functions return plain strings — edit them to
    match your team's tone and include any org-specific guidance (e.g. a link
    to your exception request process).
"""

import logging
import os

logger = logging.getLogger(__name__)

SLACK_EMAIL_DOMAIN = os.environ.get("SLACK_EMAIL_DOMAIN", "example.com")


# ── Message text ──────────────────────────────────────────────────────────────

def spend_warning_text(spend_usd: float, limit_usd: float) -> str:
    """70% warning — all models still available."""
    return (
        f"Heads up: your Bedrock spend today is *${spend_usd:.2f}* — "
        f"about 70% of your ${limit_usd:.0f} daily limit. "
        f"All models are still available. "
        f"Claude Opus will be restricted at ${limit_usd * 0.80:.0f} "
        f"and Sonnet at ${limit_usd:.0f}."
    )


def t1_blocked_text(spend_usd: float, limit_usd: float) -> str:
    """80% threshold — Opus restricted."""
    return (
        f"Your Bedrock spend has reached *${spend_usd:.2f}* (80% of your "
        f"${limit_usd:.0f} daily limit). Claude Opus has been restricted for "
        f"the rest of the day. Claude Sonnet and Haiku are still available. "
        f"Limits reset at 0400 UTC. If you need more, contact your administrator."
    )


def t2_blocked_text(spend_usd: float, limit_usd: float) -> str:
    """100% limit — Opus and Sonnet restricted."""
    return (
        f"Your Bedrock spend has reached the daily limit of *${spend_usd:.2f}* "
        f"(${limit_usd:.0f}). Claude Opus and Sonnet are restricted until 0400 UTC. "
        f"Claude Haiku remains available. "
        f"If you need an exception, contact your administrator."
    )


def slash_command_text(spend_usd: float, limit_usd: float) -> str:
    """Ephemeral /bedrock-spend response — content varies by current spend tier."""
    t1 = limit_usd * 0.80
    pct = int(spend_usd / limit_usd * 100) if limit_usd else 0

    if spend_usd >= limit_usd:
        return (
            f"Your Bedrock spend today is *${spend_usd:.2f}* — you've reached "
            f"the ${limit_usd:.0f} daily limit. Claude Opus and Sonnet are "
            f"restricted until 0400 UTC. Claude Haiku remains available."
        )
    elif spend_usd >= t1:
        return (
            f"Your Bedrock spend today is *${spend_usd:.2f}* ({pct}% of "
            f"${limit_usd:.0f}). Claude Opus is restricted for today. "
            f"Claude Sonnet and Haiku are still available; "
            f"Sonnet restricts at ${limit_usd:.0f}. Resets at 0400 UTC."
        )
    elif spend_usd >= limit_usd * 0.70:
        return (
            f"Your Bedrock spend today is *${spend_usd:.2f}* ({pct}% of "
            f"${limit_usd:.0f}). All models are available. "
            f"Opus restricts at ${t1:.0f}, Sonnet at ${limit_usd:.0f}."
        )
    return (
        f"Your Bedrock spend today is *${spend_usd:.2f}* ({pct}% of "
        f"${limit_usd:.0f}). All models are available."
    )


# ── Slack delivery ────────────────────────────────────────────────────────────

def _resolve_slack_user_id(username: str) -> str | None:
    """Resolve SSO username → Slack user ID via email lookup."""
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    email = f"{username}@{SLACK_EMAIL_DOMAIN}"
    try:
        resp = client.users_lookupByEmail(email=email)
        return resp["user"]["id"]
    except SlackApiError:
        logger.exception("Could not resolve Slack user for %s (%s)", username, email)
        return None
    except Exception:
        logger.exception("Could not resolve Slack user for %s (%s)", username, email)
        return None


def send_dm(username: str, text: str) -> None:
    """Send a DM to an SSO username. Silently skips if user cannot be resolved."""
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    user_id = _resolve_slack_user_id(username)
    if not user_id:
        return
    client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    try:
        client.chat_postMessage(channel=user_id, text=text)
        logger.info("Sent spend DM to %s (%s)", username, user_id)
    except SlackApiError:
        logger.exception("Failed to DM %s", username)
    except Exception:
        logger.exception("Failed to DM %s", username)
