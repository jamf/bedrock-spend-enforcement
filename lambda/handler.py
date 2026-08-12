"""
Bedrock spend enforcement handler.

Runs every 15 minutes via EventBridge. Queries the bedrock_cost_today Athena
view, cross-references the DynamoDB exceptions table for custom limits, and
rewrites both shared CMPs (BedrockEnforcement-T1 and -T2) with the current
list of users over their spend threshold.

Idempotent by construction: each run rebuilds the full restricted-user list
from scratch. Running twice produces the same result; missing a run catches up
on the next. The daily reset is implicit — the Athena view windows to today's
reset boundary, so once it rolls over, the next run's lists shrink and
restrictions lift automatically on the next CreatePolicyVersion call.

CUSTOMIZE
  - OPUS_MODEL_IDS / SONNET_MODEL_IDS: add or remove model IDs as new models
    are enabled. Mirror changes in bootstrap/cross-account-iam-trust.yaml.
  - DEFAULT_DAILY_LIMIT: default daily spend cap in USD.
  - T1_THRESHOLD_RATIO / T2_THRESHOLD_RATIO: fraction of daily limit that
    triggers each tier. T1 denies Opus; T2 denies Sonnet.
  - REGION: the AWS region where your Bedrock models live.
  - All other tunables are environment variables — see the CloudFormation
    template in infra/lambda.yaml.
"""

import boto3
import json
import logging
import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from collections.abc import Iterable
from typing import Any, Protocol

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ATHENA_DATABASE = os.environ.get("ATHENA_DATABASE", "default")
COST_VIEW = os.environ.get("COST_VIEW", "bedrock_cost_today")
ATHENA_WORKGROUP = os.environ.get("ATHENA_WORKGROUP", "primary")
ATHENA_RESULTS_BUCKET = os.environ["ATHENA_RESULTS_BUCKET"]

STATE_TABLE = os.environ.get("STATE_TABLE", "bedrock-spend-enforcement-state")
EXCEPTIONS_TABLE = os.environ.get("EXCEPTIONS_TABLE", "bedrock-spend-enforcement-exceptions")
ENFORCEMENT_ACCOUNT = os.environ["ENFORCEMENT_ACCOUNT"]

DEFAULT_DAILY_LIMIT = float(os.environ.get("DEFAULT_DAILY_LIMIT", "150"))
T1_THRESHOLD_RATIO = float(os.environ.get("T1_THRESHOLD_RATIO", "0.80"))  # deny Opus at 80%

REGION = os.environ.get("AWS_REGION", "us-east-1")

# Model IDs for Deny statements.
# Each ID generates both a foundation-model ARN and cross-region inference
# profile ARNs (us.* and global.*) — engineers typically invoke via inference
# profiles, so both must be denied.
#
# CUSTOMIZE: update these lists when new model versions are enabled.
# Mirror any changes in bootstrap/cross-account-iam-trust.yaml.
OPUS_MODEL_IDS = [
    "anthropic.claude-opus-4-5-20251101-v1:0",
    "anthropic.claude-opus-4-1-20250805-v1:0",
]

SONNET_MODEL_IDS = [
    "anthropic.claude-sonnet-4-5-20250929-v1:0",
    "anthropic.claude-sonnet-4-20250514-v1:0",
    "anthropic.claude-3-sonnet-20240229-v1:0",
    "anthropic.claude-3-sonnet-20240229-v1:0:28k",
    "anthropic.claude-3-sonnet-20240229-v1:0:200k",
]

# Both invoke and converse paths must be denied so a user cannot bypass
# enforcement by switching from InvokeModel to the Converse API.
DENY_ACTIONS = [
    "bedrock:InvokeModel",
    "bedrock:InvokeModelWithResponseStream",
    "bedrock:Converse",
    "bedrock:ConverseStream",
]

T1_POLICY_ARN = f"arn:aws:iam::{ENFORCEMENT_ACCOUNT}:policy/BedrockEnforcement-T1"
T2_POLICY_ARN = f"arn:aws:iam::{ENFORCEMENT_ACCOUNT}:policy/BedrockEnforcement-T2"
MAX_POLICY_SIZE = 6144


class _IAMClient(Protocol):
    def list_policy_versions(self, **kwargs: object) -> dict[str, Any]: ...
    def delete_policy_version(self, **kwargs: object) -> dict[str, Any]: ...
    def create_policy_version(self, **kwargs: object) -> dict[str, Any]: ...


class _AthenaQueryClient(Protocol):
    def get_query_execution(self, **kwargs: object) -> dict[str, Any]: ...


# ── Threshold logic ───────────────────────────────────────────────────────────

def calculate_required_denies(spend_usd: float, daily_limit: float) -> list[str]:
    if spend_usd >= daily_limit:
        return ["opus", "sonnet"]
    elif spend_usd >= daily_limit * T1_THRESHOLD_RATIO:
        return ["opus"]
    return []


def calculate_notification_tier(spend_usd: float, daily_limit: float) -> str | None:
    if spend_usd >= daily_limit:
        return "t2"
    elif spend_usd >= daily_limit * T1_THRESHOLD_RATIO:
        return "t1"
    elif spend_usd >= daily_limit * 0.70:
        return "warn"
    return None


# ── IAM policy document ───────────────────────────────────────────────────────

def _model_arns(model_ids: list[str]) -> list[str]:
    """Return both ARN forms for each model ID.

    Engineers invoke via cross-region inference profiles, not direct
    foundation-model ARNs. Both forms must be denied.
    """
    arns = []
    for m in model_ids:
        arns.append(f"arn:aws:bedrock:{REGION}::foundation-model/{m}")
        arns.append(f"arn:aws:bedrock:*:*:inference-profile/*.{m}")
    return arns


def build_shared_policy_document(model_ids: list[str], usernames: list[str]) -> dict:
    # saml:sub is the SAML NameID claim set by Identity Center from your IdP.
    # Its value is the short username matching the Athena view's person column.
    # Do NOT use aws:RoleSessionName — it is not populated for IAM Identity
    # Center SSO sessions and a Deny on it will silently match no one.
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "DenyModels",
                "Effect": "Deny",
                "Action": DENY_ACTIONS,
                "Resource": _model_arns(model_ids),
                "Condition": {
                    "StringEquals": {
                        "saml:sub": usernames,
                    }
                },
            }
        ],
    }


# ── AWS clients ───────────────────────────────────────────────────────────────

def _athena():
    return boto3.client("athena")


def _dynamodb():
    return boto3.resource("dynamodb")


def _assume_enforcement_role(account_id: str) -> _IAMClient:
    sts = boto3.client("sts")
    role_arn = f"arn:aws:iam::{account_id}:role/bedrock-spend-enforcement"
    assumed = sts.assume_role(RoleArn=role_arn, RoleSessionName="bedrock-spend-enforcement")
    creds = assumed["Credentials"]
    return boto3.client(  # type: ignore[return-value]
        "iam",
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )


# ── Athena ────────────────────────────────────────────────────────────────────

def _wait_for_athena_query(client: _AthenaQueryClient, execution_id: str) -> None:
    """Poll until the query succeeds, fails, or exceeds the 4-minute deadline.

    Athena is asynchronous — StartQueryExecution returns immediately and you
    must poll GetQueryExecution until State is SUCCEEDED (or FAILED).
    """
    deadline = time.time() + 240
    while True:
        if time.time() > deadline:
            raise RuntimeError(f"Athena query {execution_id} timed out after 240s")
        status = client.get_query_execution(QueryExecutionId=execution_id)
        state = status["QueryExecution"]["Status"]["State"]
        if state == "SUCCEEDED":
            return
        if state in ("FAILED", "CANCELLED"):
            reason = status["QueryExecution"]["Status"].get("StateChangeReason", "unknown")
            raise RuntimeError(f"Athena query {state}: {reason}")
        time.sleep(2)


def _accumulate_row(spend: dict[str, float], headers: list[str], row: dict[str, Any]) -> None:
    values = [col.get("VarCharValue") for col in row["Data"]]
    record = dict(zip(headers, values))
    person = record.get("person")
    raw_spend = record.get("spend")
    if not person or raw_spend in (None, ""):
        return
    try:
        spend[person] = spend.get(person, 0.0) + float(raw_spend)
    except (ValueError, TypeError):
        logger.warning("Skipping unparseable spend %r for %s", raw_spend, person)


def _parse_spend_results(pages: Iterable[dict[str, Any]]) -> dict[str, float]:
    spend: dict[str, float] = {}
    headers: list[str] | None = None
    for page in pages:
        result_rows = page["ResultSet"]["Rows"]
        if headers is None:
            if not result_rows:
                continue
            headers = [col.get("VarCharValue", "") for col in result_rows[0]["Data"]]
            result_rows = result_rows[1:]
        for row in result_rows:
            _accumulate_row(spend, headers, row)
    return spend


def query_spend_by_person() -> dict[str, float]:
    """Return {person: total_usd_spend_today} for human users from the cost view."""
    query = (
        f"SELECT person, SUM(estimated_cost) AS spend "
        f"FROM {ATHENA_DATABASE}.{COST_VIEW} "
        f"WHERE usage_type = 'human' "
        f"GROUP BY person"
    )
    client = _athena()
    response = client.start_query_execution(
        QueryString=query,
        QueryExecutionContext={"Database": ATHENA_DATABASE},
        ResultConfiguration={"OutputLocation": ATHENA_RESULTS_BUCKET},
        WorkGroup=ATHENA_WORKGROUP,
    )
    execution_id = response["QueryExecutionId"]
    _wait_for_athena_query(client, execution_id)
    paginator = client.get_paginator("get_query_results")
    pages = paginator.paginate(QueryExecutionId=execution_id)
    return _parse_spend_results(pages)


# ── DynamoDB ──────────────────────────────────────────────────────────────────

def _read_item(table_name: str, user_id: str) -> dict[str, Any]:
    """Fetch one item by user_id, returning {} on miss or error.

    Fails soft: a lookup error yields {} so each derive-helper applies its own
    safe default. This must never raise — the handler loop reads each table's
    item once and derives several values, so a raise here would drop the user
    from both deny lists (fail-open) instead of degrading one value gracefully.
    """
    try:
        return _dynamodb().Table(table_name).get_item(Key={"user_id": user_id}).get("Item") or {}
    except Exception:
        logger.exception("%s lookup failed for %s", table_name, user_id)
        return {}


def _limit_from_item(item: dict[str, Any]) -> float:
    try:
        if "daily_limit_usd" in item:
            return float(item["daily_limit_usd"])
    except (ValueError, TypeError):
        logger.exception("Unparseable daily_limit_usd %r, using default", item.get("daily_limit_usd"))
    return DEFAULT_DAILY_LIMIT


def _blocked_from_item(item: dict[str, Any]) -> bool:
    if not item.get("manual_block"):
        return False
    blocked_until = item.get("blocked_until")
    if blocked_until:
        try:
            return datetime.fromisoformat(blocked_until) > datetime.now(timezone.utc)
        except (ValueError, TypeError):
            logger.exception("Malformed blocked_until %r — treating as not blocked", blocked_until)
            return False
    return True


def _notified_tiers_from_item(item: dict[str, Any] | None) -> set[str]:
    if item and "notified_tiers" in item:
        return set(item["notified_tiers"])
    return set()


def get_user_limit(user_id: str) -> float:
    return _limit_from_item(_read_item(EXCEPTIONS_TABLE, user_id))


def _save_user_state(
    user_id: str,
    spend_usd: float,
    active_denies: list[str],
    notified_tiers: set[str],
) -> None:
    table = _dynamodb().Table(STATE_TABLE)
    item: dict[str, Any] = {
        "user_id": user_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "spend_usd": Decimal(str(round(spend_usd, 4))),
        "active_denies": active_denies,
    }
    if notified_tiers:
        item["notified_tiers"] = notified_tiers
    table.put_item(Item=item)


def _get_notified_tiers(user_id: str) -> set[str]:
    return _notified_tiers_from_item(_read_item(STATE_TABLE, user_id))


# ── IAM policy version management ────────────────────────────────────────────

def _ensure_policy_version_slots(iam_client: _IAMClient, policy_arn: str) -> None:
    """Delete the oldest non-default version if at the IAM 5-version limit.

    IAM managed policies retain at most 5 versions. This must run before every
    CreatePolicyVersion call or the call will fail. At a 15-minute cadence the
    Lambda writes ~96 new versions per day, so this runs on almost every invocation.
    """
    versions = iam_client.list_policy_versions(PolicyArn=policy_arn)["Versions"]
    if len(versions) < 5:
        return
    non_default = [v for v in versions if not v["IsDefaultVersion"]]
    if not non_default:
        raise RuntimeError(f"Cannot free a version slot for {policy_arn}: all versions are default")
    oldest = sorted(non_default, key=lambda v: v["CreateDate"])[0]
    iam_client.delete_policy_version(PolicyArn=policy_arn, VersionId=oldest["VersionId"])


def write_shared_policy(
    policy_arn: str, model_ids: list[str], usernames: list[str], iam_client: _IAMClient
) -> None:
    """Write a new default version of a shared deny CMP.

    Always called — even with an empty username list — so users who are no
    longer over threshold get un-denied on the next run.
    """
    doc = json.dumps(build_shared_policy_document(model_ids, usernames), separators=(",", ":"))
    if len(doc) > MAX_POLICY_SIZE:
        # IAM policies cap at 6,144 bytes (~280-300 usernames with this model list).
        # Skip the write and emit a metric rather than truncating silently.
        # Add a CloudWatch alarm on this metric before approaching the ceiling.
        logger.error(
            "CMP_WRITE_SKIPPED_OVERSIZE: %s exceeds %d bytes (%d bytes, %d users) — "
            "previous deny version remains in force for this tier",
            policy_arn, MAX_POLICY_SIZE, len(doc), len(usernames),
        )
        _emit_oversize_metric(policy_arn, len(usernames))
        return
    _ensure_policy_version_slots(iam_client, policy_arn)
    iam_client.create_policy_version(
        PolicyArn=policy_arn,
        PolicyDocument=doc,
        SetAsDefault=True,
    )
    logger.info("Updated %s with %d users", policy_arn, len(usernames))


def _emit_oversize_metric(policy_arn: str, user_count: int) -> None:
    try:
        tier = policy_arn.rsplit("/", 1)[-1]
        boto3.client("cloudwatch").put_metric_data(
            Namespace="BedrockSpendEnforcement",
            MetricData=[{
                "MetricName": "PolicySizeOverflow",
                "Dimensions": [{"Name": "Tier", "Value": tier}],
                "Value": float(user_count),
                "Unit": "Count",
            }],
        )
    except Exception as exc:
        logger.error("Failed to emit PolicySizeOverflow metric: %s", exc)


def update_shared_cmps(
    t1_users: list[str],
    t2_users: list[str],
    iam_client: _IAMClient,
    *,
    t1_policy_arn: str = T1_POLICY_ARN,
    t2_policy_arn: str = T2_POLICY_ARN,
) -> None:
    """Rewrite both shared CMPs. Each write is independent."""
    try:
        write_shared_policy(t1_policy_arn, OPUS_MODEL_IDS, t1_users, iam_client)
    except Exception as exc:
        logger.error("Failed to update T1 CMP: %s", exc, exc_info=True)
    try:
        write_shared_policy(t2_policy_arn, SONNET_MODEL_IDS, t2_users, iam_client)
    except Exception as exc:
        logger.error("Failed to update T2 CMP: %s", exc, exc_info=True)


# ── Notification ──────────────────────────────────────────────────────────────

def _notify_if_new_tier(
    person: str,
    spend_usd: float,
    limit: float,
    notified_tiers: set[str],
) -> set[str]:
    """Send a Slack DM if the user has crossed a tier they haven't been notified about.

    Returns the updated notified_tiers set. Skipped if SLACK_BOT_TOKEN is absent.
    """
    if not os.environ.get("SLACK_BOT_TOKEN"):
        return notified_tiers

    from notifier import send_dm, spend_warning_text, t1_blocked_text, t2_blocked_text

    tier = calculate_notification_tier(spend_usd, limit)
    if tier is None:
        return set()  # spend dropped below warning — reset for next day

    _TIER_ORDER = ["warn", "t1", "t2"]
    new_tiers = notified_tiers.copy()
    for t in _TIER_ORDER:
        if t not in notified_tiers and _TIER_ORDER.index(t) <= _TIER_ORDER.index(tier):
            if t == "warn":
                text = spend_warning_text(spend_usd, limit)
            elif t == "t1":
                text = t1_blocked_text(spend_usd, limit)
            else:
                text = t2_blocked_text(spend_usd, limit)
            send_dm(person, text)
            new_tiers.add(t)

    return new_tiers


# ── Entry point ───────────────────────────────────────────────────────────────

def handler(event: dict[str, Any], context: object) -> None:
    """Lambda entry point: query today's spend, rewrite enforcement CMPs."""
    spend_by_person = query_spend_by_person()
    iam_client = _assume_enforcement_role(ENFORCEMENT_ACCOUNT)

    t1_users: list[str] = []
    t2_users: list[str] = []

    for person, spend_usd in spend_by_person.items():
        try:
            exc_item = _read_item(EXCEPTIONS_TABLE, person)
            state_item = _read_item(STATE_TABLE, person)

            limit = _limit_from_item(exc_item)
            if _blocked_from_item(exc_item):
                required_denies = ["opus", "sonnet"]
            else:
                required_denies = calculate_required_denies(spend_usd, limit)

            if "opus" in required_denies:
                t1_users.append(person)
            if "sonnet" in required_denies:
                t2_users.append(person)

            notified_tiers = _notified_tiers_from_item(state_item)
            notified_tiers = _notify_if_new_tier(person, spend_usd, limit, notified_tiers)
            _save_user_state(person, spend_usd, required_denies, notified_tiers)
        except Exception as exc:
            logger.error("Error processing user %s: %s", person, exc, exc_info=True)

    update_shared_cmps(t1_users, t2_users, iam_client)
