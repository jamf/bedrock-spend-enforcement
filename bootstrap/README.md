# bootstrap/

This template is deployed **manually** in your enforcement account (the account
where IAM Identity Center users live). It is not part of any CI/CD pipeline.

## What it creates

- **BedrockEnforcement-T1** — IAM Customer Managed Policy that denies Opus-tier models.
  Seeded with a placeholder condition that matches no user; the Lambda rewrites it on
  every run.
- **BedrockEnforcement-T2** — Same, for Sonnet-tier models.
- **bedrock-spend-enforcement** — Cross-account IAM role the Lambda assumes (from your
  Lambda account) to call `iam:CreatePolicyVersion` on both CMPs.

## Deploy order

1. Deploy `infra/lambda.yaml` first (in your Lambda account). Get the
   `LambdaExecutionRoleArn` from that stack's Outputs.

2. Deploy this template in your **enforcement account**:

```bash
aws cloudformation deploy \
  --template-file bootstrap/cross-account-iam-trust.yaml \
  --stack-name bedrock-spend-enforcement \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    LambdaExecutionRoleArn=arn:aws:iam::<LAMBDA_ACCOUNT_ID>:role/bedrock-spend-enforcement-lambda \
    AwsRegion=us-east-1 \
  --region us-east-1
```

3. Attach **both CMP names** to your IAM Identity Center permission set:
   - `BedrockEnforcement-T1`
   - `BedrockEnforcement-T2`

   Identity Center resolves policies by name at session time. This triggers a
   one-time re-provisioning of the permission set — expected and required.

## Keeping model lists in sync

The Resource ARNs in `cross-account-iam-trust.yaml` must stay in sync with
`OPUS_MODEL_IDS` and `SONNET_MODEL_IDS` in `lambda/handler.py`. When you add a
new model, update both files and re-deploy this stack so the seed policy covers
the new model even before the Lambda's first run.

## Why this is manual

The Lambda account's CI/CD role doesn't have credentials in the enforcement
account. This template is in source control so changes go through code review;
the `aws cloudformation deploy` is run by hand by someone with admin access to
the enforcement account.
