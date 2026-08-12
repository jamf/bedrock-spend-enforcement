"""Shared test setup for lambda/tests.

Two concerns are handled here, both needed before any test module can safely
`import handler`:

1. ATHENA_RESULTS_BUCKET is a required environment variable (no hardcoded
   default — see handler.py). Set a placeholder before handler is imported so
   collection doesn't blow up with a KeyError.

2. handler.T1_POLICY_ARN / handler.T2_POLICY_ARN resolve the running AWS
   account via STS at import time (see handler._enforcement_account_id), so a
   reader can deploy this Lambda into their own account with zero extra
   config. Tests must never depend on — or accidentally call out to — a real
   AWS account, so boto3.client is patched just long enough for that one
   import to resolve deterministically to the standard example account ID,
   then restored to the real implementation for everything else.

3. notifier.py belongs to a separate porting task and may not exist on disk
   yet in this worktree. handler._notify_if_new_tier does a local
   `from notifier import ...` at call time, and every test that exercises it
   patches those names directly (e.g. patch("notifier.send_dm")), which
   requires an importable `notifier` module to attach the patch to. If the
   real module isn't present, register a minimal stub in sys.modules so
   those patches have something to replace — every test already mocks every
   notifier call it makes, so the stub's own (deliberately-failing) bodies
   are never actually exercised. Once the real notifier.py lands, a plain
   `import notifier` succeeds and this stub is skipped entirely.
"""
import os
import sys
import types
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("ATHENA_RESULTS_BUCKET", "s3://example-athena-results/")

try:
    import notifier  # noqa: F401
except ImportError:

    def _unimplemented(*args: object, **kwargs: object) -> None:
        raise NotImplementedError(
            "notifier stub called without being mocked — see lambda/tests/conftest.py"
        )

    _notifier_stub = types.ModuleType("notifier")
    _notifier_stub.DEFAULT_ADDRESS = "Sir"  # type: ignore[attr-defined]
    for _name in (
        "get_address_by_username",
        "get_address_by_slack_id",
        "send_dm",
        "post_to_channel",
        "spend_warning_text",
        "t1_blocked_text",
        "t2_blocked_text",
        "resolve_slack_id_with_status",
    ):
        setattr(_notifier_stub, _name, _unimplemented)
    sys.modules["notifier"] = _notifier_stub


def _fake_sts_client(service_name: str, *args: object, **kwargs: object) -> MagicMock:
    """Stand in for boto3.client during handler's module-level STS lookup."""
    if service_name == "sts":
        sts = MagicMock()
        sts.get_caller_identity.return_value = {"Account": "123456789012"}
        return sts
    raise AssertionError(f"unexpected boto3.client({service_name!r}) call during handler import")


with patch("boto3.client", side_effect=_fake_sts_client):
    import handler  # noqa: F401  (resolves T1_POLICY_ARN/T2_POLICY_ARN once, deterministically)
