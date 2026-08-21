# CLAUDE.md — bedrock-spend-enforcement

Read this before touching any code in a new session.

## Purpose

Tiered, real-time spend enforcement for Amazon Bedrock. A serverless pipeline
measures per-user Bedrock spend from model invocation logs and automatically
restricts access to higher-cost models — by rewriting two shared IAM Customer
Managed Policies (CMPs) — as each user approaches a daily budget.

Enforcement is graduated rather than a hard on/off switch: Claude Opus is
withdrawn first (at 80% of budget), Sonnet follows only at the hard limit
(100%), and Haiku is never blocked, so a capped user always has a working
fallback. The loop is **Measure (Athena over Bedrock logs) → Decide
(per-user budget check) → Enforce (rewrite CMPs) → Notify (Slack)**, run every
15 minutes by an EventBridge schedule. A companion Slack slash-command app
lets users check spend and lets admins grant time-boxed exceptions.

This is a simplified, single-account reference implementation that accompanies
the AWS blog post *"Tokenomics at scale: How Jamf built real-time spend
enforcement for Amazon Bedrock"* — not the exact production system. See
"Differences from production" in the [README](README.md).

Note: this is a **public** repository (MIT licensed).

## Key CFN Stacks

Everything deploys into **one AWS account** — there is no cross-account role
assumption anywhere in this sample. Deploy in the order below; each later
stack consumes an output of an earlier one (lambda before athena is
intentional — athena needs the Athena-results bucket lambda creates).

| Stack (template) | Suggested stack name | Purpose |
|---|---|---|
| `infra/dynamodb.yaml` | `bedrock-spend-enforcement-dynamodb` | DynamoDB `StateTable` (per-user spend state) and `ExceptionsTable` (manual spend-limit exceptions). No parameters. |
| `infra/lambda.yaml` | `bedrock-spend-enforcement-lambda` | Enforcement Lambda + IAM execution role, the Athena-results S3 bucket, the 15-minute EventBridge trigger, and a CloudWatch alarm for the IAM managed-policy size ceiling. |
| `infra/athena.yaml` | `bedrock-spend-enforcement-athena` | Glue source table (`bedrocklogs_metadata_clean`) over exported Bedrock invocation logs and the Athena cost view (`default.bedrock_cost_today`). View SQL is embedded; `sql/bedrock_cost_today.sql` is the read-only reference copy — keep both in sync. |
| `infra/slash-command.yaml` | `bedrock-spend-enforcement-slash-command` | Slack slash-command Lambda + API Gateway HTTP API. Routes: `/bedrock-spend`, `/bedrock-block`, `/bedrock-unblock`, `/bedrock-limit`, `/bedrock-interact` (button callbacks). |

The two `BedrockEnforcement-T1` (Opus deny) and `BedrockEnforcement-T2`
(Sonnet deny) IAM CMPs are **prerequisites** — they are not created by any
stack. The Lambda's role is scoped to write only those two ARNs.

Lambda runtime is Python 3.12. Both Lambdas share one deployment zip
(`handler.py`, `slash_command.py`, and the shared `notifier.py`); only
`slack-sdk` must be bundled (`boto3`/`botocore` ship in the runtime).

## Deploy Instructions

Deployment is manual `aws cloudformation deploy` today — there is no CD
workflow in this repo. Full, copy-pasteable commands and the complete
parameter tables for each stack live in the [README](README.md) under
"Lambda packaging" and "Deploying". In short:

1. Package and upload the Lambda zip (`pip install slack-sdk -t build/`, copy
   the three `lambda/*.py` files, zip, `aws s3 cp` to your code bucket).
2. `deploy` `infra/dynamodb.yaml` → note `StateTableArn` / `ExceptionsTableArn`.
3. `deploy` `infra/lambda.yaml` (needs `CAPABILITY_NAMED_IAM`) → note
   `AthenaResultsBucketName`.
4. `deploy` `infra/athena.yaml` (needs `CAPABILITY_NAMED_IAM`).
5. `deploy` `infra/slash-command.yaml` (needs `CAPABILITY_NAMED_IAM`) → paste
   its output URLs into the Slack app config.

Cleanup is the reverse order (see README "Cleanup"); the two prerequisite CMPs
must be deleted by hand.

### Tests / lint

```bash
pip install -r lambda/requirements.txt
pytest lambda/tests
cfn-lint infra/*.yaml   # config in .cfnlintrc.yaml
```

## Tagging Requirements

Per the [Jamf Cloud Tagging Standard (2026)](https://jamfsoftware.atlassian.net/wiki/spaces/CA/pages/6477774940),
CFN stacks should carry these tags:

| Tag | Value |
|-----|-------|
| `owner` | `cloud-cornerstones` |
| `owner-email` | `cornerstones@jamf.com` |
| `environment` | one of `sbox` / `dev` / `stage` / `prod` / `cicd` / `org` |
| `portal.jamf.build/component` | `bedrock-spend-enforcement` |

Status: the `infra/*.yaml` templates do **not** currently declare these tags.
Adding stack-level tags to this public sample is tracked separately from
CORNER-1519 (that ticket scoped this repo to SonarQube + CLAUDE.md only) and
is out of scope for the current change.

## SonarQube

Project key: `com.jamf.cloud-cornerstones:bedrock-spend-enforcement`
(set in `sonar-project.properties`; analysis runs via
`.github/workflows/sonarqube.yml`).

The SonarQube project must be created via the Backstage "Create a SonarQube
Project" template at [portal.jamf.build](https://portal.jamf.build) before the
scan can publish results — until then the scan check will fail, which is
expected and not a code problem.

Because this is a **public** repository, the catalog entity (including the
`sonarqube.org/project-key` annotation) is registered in `public-entities.yaml`
in [jamf/portal-domains-systems](https://github.com/jamf/portal-domains-systems),
**not** in a `catalog-info.yaml` committed here — per the Jamf software-catalog
paved road, public repos must not carry `catalog-info.yaml`.

## Owner

- **Team:** cloud-cornerstones
- **Slack:** [#ask-cornerstones](https://jamf.slack.com/channels/ask-cornerstones)
- **Email:** cornerstones@jamf.com
- **Manager:** Levi McCormick
