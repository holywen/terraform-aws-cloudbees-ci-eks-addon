#!/bin/sh
# Chargeback lab: query CUR 2.0 (Athena) for the cluster's costs that Kubecost does not see.
#
# One CSV per day: /out/cur/cur-external-YYYY-MM-DD.csv with columns category, resource_id, cost.
# Excluded, because Kubecost already counts them: EC2 instance compute (BoxUsage/SpotUsage).
# EBS volumes are returned per volume; the export drops the ones Kubecost already counts as PVs.
# The cluster's resources are selected by /config/cur-selector.sql (a SQL condition).
#
# Env: ATHENA_WORKGROUP, CUR_TABLE (db.table), DAYS_BACK (default 3), DAY (YYYY-MM-DD, overrides).
set -u

DAYS_BACK="${DAYS_BACK:-3}"
SELECTOR="$(cat /config/cur-selector.sql)"
mkdir -p /out/cur

if [ -n "${DAY:-}" ]; then
  DAYS="$DAY"
else
  DAYS=""
  i=1
  while [ "$i" -le "$DAYS_BACK" ]; do
    DAYS="$DAYS $(date -u -d "-$i day" +%F)"
    i=$((i + 1))
  done
fi

for day in $DAYS; do
  period=$(echo "$day" | cut -c1-7)
  query="SELECT
    CASE
      WHEN line_item_resource_id LIKE '%file-system/%' THEN 'efs'
      WHEN line_item_product_code = 'AmazonEKS' THEN 'eks'
      WHEN line_item_usage_type LIKE '%NatGateway%' THEN 'nat'
      WHEN line_item_product_code = 'AWSELB' THEN 'elb'
      WHEN line_item_resource_id LIKE 'vol-%' THEN 'ebs'
      WHEN line_item_resource_id LIKE 'i-%' THEN 'node_other'
      ELSE lower(line_item_product_code)
    END AS category,
    CASE WHEN line_item_resource_id LIKE 'vol-%' THEN line_item_resource_id ELSE '' END AS resource_id,
    sum(line_item_unblended_cost) AS cost
  FROM ${CUR_TABLE}
  WHERE billing_period = '${period}'
    AND date(line_item_usage_start_date) = date '${day}'
    AND line_item_line_item_type = 'Usage'
    AND NOT (line_item_resource_id LIKE 'i-%'
             AND (line_item_usage_type LIKE '%BoxUsage%' OR line_item_usage_type LIKE '%SpotUsage%'))
    AND (${SELECTOR})
  GROUP BY 1, 2"

  qid=$(aws athena start-query-execution --work-group "$ATHENA_WORKGROUP" \
    --query-string "$query" --query QueryExecutionId --output text) || { echo "$day: start failed"; continue; }
  state=QUEUED
  while [ "$state" = QUEUED ] || [ "$state" = RUNNING ]; do
    sleep 3
    state=$(aws athena get-query-execution --query-execution-id "$qid" \
      --query QueryExecution.Status.State --output text)
  done
  if [ "$state" != SUCCEEDED ]; then
    echo "$day: CUR query $state: $(aws athena get-query-execution --query-execution-id "$qid" \
      --query QueryExecution.Status.StateChangeReason --output text)"
    continue
  fi
  location=$(aws athena get-query-execution --query-execution-id "$qid" \
    --query QueryExecution.ResultConfiguration.OutputLocation --output text)
  aws s3 cp "$location" "/out/cur/cur-external-${day}.csv" --only-show-errors
  echo "$day: $(($(wc -l < "/out/cur/cur-external-${day}.csv") - 1)) CUR rows"
done
exit 0
