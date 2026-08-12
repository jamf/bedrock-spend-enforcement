-- bedrock_cost_today — Athena view that prices every Bedrock invocation and
-- windows it to the current daily reset boundary.
--
-- PREREQUISITES
--   A Glue/Athena table over your Bedrock invocation logs S3 prefix.
--   Bedrock invocation logging must be enabled and delivering JSON to S3.
--   Replace <YOUR_LOG_TABLE> with your Glue database and table name,
--   e.g. bedrocklogsdb.bedrock_invocation_logs.
--
-- CUSTOMIZE
--   1. Replace <YOUR_LOG_TABLE> with your table name.
--   2. Update the CASE pricing branches to current Bedrock rates for your Region:
--      https://aws.amazon.com/bedrock/pricing/
--      Rates below are illustrative — verify before deploying.
--   3. Adjust RESET_HOUR if you want a different daily boundary (default: 0400 UTC).
--   4. The 'human' / 'service' classification uses a firstname.lastname pattern.
--      Adjust the regexp if your SSO usernames follow a different convention.
--
-- USAGE
--   The enforcement Lambda queries this view with:
--     SELECT person, SUM(estimated_cost) AS spend
--     FROM bedrock_cost_today
--     WHERE usage_type = 'human'
--     GROUP BY person
--
-- PERFORMANCE NOTE (from production)
--   Bedrock invocation logs are row-oriented JSON. Athena must deserialize
--   each full row before it can apply any filter — column pruning and
--   predicate pushdown do not reduce bytes scanned. Combine all derived
--   queries into one SELECT ... GROUP BY and split results in application
--   code so the scan happens once per Lambda run instead of once per consumer.
--   For further cost reduction, convert source logs to Parquet via Firehose
--   format conversion or a periodic Glue ETL job.

CREATE OR REPLACE VIEW bedrock_cost_today AS
WITH priced AS (
  SELECT
    -- SSO session name is the last segment of the STS ARN, e.g. "jane.doe"
    element_at(split(identity.arn, '/'), -1)                    AS person,
    modelid,

    -- Classify human vs. service/CI identity.
    -- Adjust the regexp to match your SSO username format.
    CASE
      WHEN regexp_like(
             element_at(split(identity.arn, '/'), -1),
             '^[a-z]+(-[a-z]+)*\.[a-z]+(-[a-z]+)*$'
           )
      THEN 'human'
      ELSE 'service'
    END                                                          AS usage_type,

    -- Estimated cost in USD. Update rates to current Bedrock pricing for your
    -- Region. The ELSE branch prices unknown models at the highest tier so that
    -- a new model can never bypass enforcement at $0 — add its real rate and
    -- redeploy the view as soon as a new model is enabled.
    (
      -- Input tokens
      CASE
        WHEN modelid LIKE '%claude-opus%'   THEN input.inputTokenCount * 0.015
        WHEN modelid LIKE '%claude-sonnet%' THEN input.inputTokenCount * 0.003
        WHEN modelid LIKE '%claude-haiku%'  THEN input.inputTokenCount * 0.0008
        ELSE                                     input.inputTokenCount * 0.015
      END
      -- Output tokens
      + CASE
          WHEN modelid LIKE '%claude-opus%'   THEN output.outputtokencount * 0.075
          WHEN modelid LIKE '%claude-sonnet%' THEN output.outputtokencount * 0.015
          WHEN modelid LIKE '%claude-haiku%'  THEN output.outputtokencount * 0.004
          ELSE                                     output.outputtokencount * 0.075
        END
      -- Cache read tokens (~10% of standard input rate)
      + COALESCE(input.cacheReadInputTokenCount, 0)  * 0.0015
      -- Cache write tokens (~125% of standard input rate for 5-min cache)
      + COALESCE(input.cacheWriteInputTokenCount, 0) * 0.01875
    ) / 1000.0                                                   AS estimated_cost

  FROM <YOUR_LOG_TABLE>

  -- Rolling daily window. Adjust the hour offset to your reset boundary.
  -- Default: resets at 0400 UTC daily.
  WHERE from_iso8601_timestamp("timestamp")
        >= date_add(
             'hour',
             4,  -- RESET_HOUR: change this to shift the reset boundary
             date_trunc('day', current_timestamp AT TIME ZONE 'UTC')
           )
        OR (
             from_iso8601_timestamp("timestamp")
             >= date_trunc('day', current_timestamp AT TIME ZONE 'UTC')
             AND current_time AT TIME ZONE 'UTC' < time '04:00'
           )
)
SELECT person, usage_type, modelid, estimated_cost
FROM priced;
