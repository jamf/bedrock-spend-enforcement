# bedrock-spend-enforcement

Tiered, real-time spend enforcement for Amazon Bedrock using IAM Customer Managed Policies, an Athena cost view, and a serverless Lambda loop.

This is the reference implementation accompanying the AWS blog post [Tokenomics at scale: How Jamf built real-time spend enforcement for Amazon Bedrock](https://aws.amazon.com/blogs/machine-learning/). It provides the pieces — read the blog for the architecture rationale and production learnings.

---

## How it works

1. **Measure** — Amazon Bedrock writes invocation logs (token counts, model ID, user identity) to S3. An Athena view (`sql/bedrock_cost_today.sql`) prices each invocation and windows it to today's reset boundary.

2. **Decide** — A Lambda runs every 15 minutes. It queries the Athena view for per-user daily spend, cross-references a DynamoDB exceptions table for custom limits, and builds two lists: users over the T1 threshold (deny Opus) and users over T2 (deny Sonnet).

3. **Enforce** — The Lambda rewrites two shared Customer Managed Policies via `iam:CreatePolicyVersion`. IAM evaluates these at API call time — no re-authentication required, restrictions take effect within minutes.

4. **Notify** — When a user crosses a threshold for the first time that day, the Lambda sends a Slack DM. Each notification fires exactly once per day per tier. Slack is optional — the enforcement Lambda works without it.

The daily reset is implicit: the Athena view scopes spend to the current day's window, so after the reset hour the Lambda's next run sees reduced spend and rewrites the CMPs with a shorter (or empty) deny list.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Lambda account                                             │
│                                                             │
│  ┌──────────────────┐   every 15 min    ┌───────────────┐  │
│  │ EventBridge rule │──────────────────▶│ Enforcement   │  │
│  └──────────────────┘                   │ Lambda        │  │
│                                         │               │  │
│  ┌──────────────────┐                   │ query Athena  │  │
│  │ Bedrock logs S3  │◀── Athena reads ──│ read DynamoDB │  │
│  └──────────────────┘                   │               │  │
│  ┌──────────────────┐                   │ assume role ──┼──┼──▶ Enforcement account
│  │ DynamoDB         │◀── state/limits ──│               │  │
│  │ (state+exceptions│                   └───────────────┘  │
│  └──────────────────┘                                       │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│  Enforcement account (IAM Identity Center users live here)  │
│                                                             │
│  BedrockEnforcement-T1  (deny Opus)    ◀── CreatePolicyVersion
│  BedrockEnforcement-T2  (deny Sonnet)  ◀── CreatePolicyVersion
│                                                             │
│  Both CMPs attached to your permission set                  │
└─────────────────────────────────────────────────────────────┘
```

---

## Repository layout

```
sql/
  bedrock_cost_today.sql       Step 1 — Athena cost view (pricing + identity classification)
lambda/
  handler.py                   Step 3 — enforcement Lambda (query → decide → enforce → notify)
  notifier.py                  Slack DM helpers
  slash_command.py             /bedrock-spend self-service + admin commands (optional)
  requirements.txt
infra/
  dynamodb.yaml                Step 1 prerequisite — state + exceptions tables
  lambda.yaml                  Step 3 — Lambda, EventBridge, Athena results bucket, alarm
  slash-command.yaml           Step 4 — Slack slash command Lambda + API Gateway (optional)
bootstrap/
  cross-account-iam-trust.yaml Step 2 — CMPs + cross-account role (manual deploy)
  README.md                    Bootstrap deploy instructions
```

---

## Prerequisites

- AWS account(s) with permissions to create IAM roles, Customer Managed Policies, Lambda functions, Athena workgroups, S3 buckets, and DynamoDB tables
- AWS IAM Identity Center with a permission set assigned to your Bedrock users
- Amazon Bedrock model invocation logging enabled, delivering JSON logs to S3
- AWS CLI configured with appropriate credentials

Slack (for notifications and slash commands) is optional. The enforcement Lambda runs without it.

---

## Deployment

### Step 1 — Create the Athena cost view

Create a Glue/Athena table over your Bedrock invocation logs S3 prefix, then run `sql/bedrock_cost_today.sql` to create the view.

**Customize before running:**
- Replace `<YOUR_LOG_TABLE>` with your Glue database and table name
- Update the pricing `CASE` branches with current [Bedrock rates](https://aws.amazon.com/bedrock/pricing/) for your Region
- Adjust `RESET_HOUR` if you want a different daily boundary (default: 0400 UTC)
- Adjust the username regexp if your SSO usernames follow a different convention

Also deploy the DynamoDB tables (no customization needed):

```bash
aws cloudformation deploy \
  --template-file infra/dynamodb.yaml \
  --stack-name bedrock-spend-enforcement-dynamodb \
  --region <your-region>
```

### Step 2 — Create the Customer Managed Policies

Deploy `bootstrap/cross-account-iam-trust.yaml` in your **enforcement account** (where IAM Identity Center users live). See [bootstrap/README.md](bootstrap/README.md) for the full command and instructions.

Then attach both `BedrockEnforcement-T1` and `BedrockEnforcement-T2` to your IAM Identity Center permission set.

### Step 3 — Deploy the enforcement Lambda

Package and upload the Lambda zip to an S3 bucket in your Lambda account:

```bash
pip install -r lambda/requirements.txt --target lambda/
cd lambda && zip -r ../lambda.zip . -x "*/tests/*" -x "*/__pycache__/*" && cd ..
aws s3 cp lambda.zip s3://<your-artifacts-bucket>/bedrock-spend-enforcement.zip
```

Deploy the stack:

```bash
aws cloudformation deploy \
  --template-file infra/lambda.yaml \
  --stack-name bedrock-spend-enforcement-lambda \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    LambdaZipKey=bedrock-spend-enforcement.zip \
    ArtifactsBucket=<your-artifacts-bucket> \
    StateDynamoTableArn=<StateTableArn from dynamodb stack output> \
    ExceptionsDynamoTableArn=<ExceptionsTableArn from dynamodb stack output> \
    BedrockLogsBucket=<your-bedrock-logs-bucket> \
    EnforcementAccountId=<your-enforcement-account-id> \
  --region <your-region>
```

Optional parameters: `SlackBotToken`, `SlackEmailDomain`, `DefaultDailyLimitUsd`, `AlarmEmail`.

### Step 4 — Add the exception workflow (optional)

Deploy the Slack slash command stack to expose `/bedrock-spend`, `/bedrock-limit`, `/bedrock-block`, and `/bedrock-unblock`:

```bash
aws cloudformation deploy \
  --template-file infra/slash-command.yaml \
  --stack-name bedrock-spend-enforcement-slash-command \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    LambdaZipKey=bedrock-spend-enforcement.zip \
    ArtifactsBucket=<your-artifacts-bucket> \
    SlackBotToken=<xoxb-...> \
    SlackSigningSecret=<your-signing-secret> \
    AdminSlackIds=<comma-separated Slack user IDs for admins> \
    StateDynamoTableArn=<StateTableArn> \
    ExceptionsDynamoTableArn=<ExceptionsTableArn> \
  --region <your-region>
```

After deploy, paste the stack Output URLs into your Slack app's slash command Request URLs and Interactivity Request URL.

---

## Customization reference

| What to change | Where |
|---|---|
| Daily spend limit | `DefaultDailyLimitUsd` parameter in `infra/lambda.yaml` |
| Tier thresholds (80%/100%) | `T1_THRESHOLD_RATIO` env var in `lambda/handler.py` |
| Model lists (which models are denied at each tier) | `OPUS_MODEL_IDS` / `SONNET_MODEL_IDS` in `lambda/handler.py` + Resource ARNs in `bootstrap/cross-account-iam-trust.yaml` |
| Daily reset hour | `RESET_HOUR` in `sql/bedrock_cost_today.sql` |
| Pricing rates | `CASE` branches in `sql/bedrock_cost_today.sql` |
| Notification message copy | `lambda/notifier.py` |
| Email domain for Slack resolution | `SlackEmailDomain` parameter / `SLACK_EMAIL_DOMAIN` env var |

---

## Production notes

- **IAM policy version limit** — IAM managed policies keep at most 5 versions. The Lambda deletes the oldest non-default version before each `CreatePolicyVersion` call. At a 15-minute cadence this happens on nearly every run.
- **Athena is async** — `StartQueryExecution` returns immediately; the Lambda polls `GetQueryExecution` until `SUCCEEDED`. The Lambda timeout is 300s and the Athena poll guard is 240s.
- **Policy size ceiling** — IAM policies cap at 6,144 bytes, which fits roughly 280–300 usernames per CMP. The Lambda emits a `PolicySizeOverflow` CloudWatch metric and skips the write if exceeded. The `infra/lambda.yaml` stack creates an alarm on this metric.
- **JSON log scan cost** — Bedrock invocation logs are row-oriented JSON; Athena scans every byte regardless of selected columns. Combine all derived queries into one `SELECT ... GROUP BY` (the Lambda already does this) and optionally convert logs to Parquet for further cost reduction.
- **`saml:sub` not `aws:RoleSessionName`** — Use `saml:sub` as the condition key. `aws:RoleSessionName` is not populated for IAM Identity Center SSO sessions; a Deny on it silently matches no one.

---

## Cleanup

Delete the CloudFormation stacks in reverse order:

```bash
aws cloudformation delete-stack --stack-name bedrock-spend-enforcement-slash-command
aws cloudformation delete-stack --stack-name bedrock-spend-enforcement-lambda
aws cloudformation delete-stack --stack-name bedrock-spend-enforcement-dynamodb
# In enforcement account:
aws cloudformation delete-stack --stack-name bedrock-spend-enforcement
```

Detach `BedrockEnforcement-T1` and `BedrockEnforcement-T2` from your permission set before or after deleting the enforcement stack.
