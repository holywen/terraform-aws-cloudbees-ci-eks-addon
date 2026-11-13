# CHARGEBACK LAB (cur-kubecost-lab branch): Grafana showback dashboards over the S3 report.
#
# The daily export also writes report-by-controller.csv and showback-allocated.csv under
# showback/tables/<table>/date=YYYY-MM-DD/. Two Glue tables read them (partition projection on the
# day, no crawler); Grafana queries them through its Athena data source with a read-only Pod
# Identity role. The data source and dashboards are ConfigMaps picked up by Grafana's sidecars.
# The Athena plugin is installed through k8s/kube-prom-stack-values.yml.

locals {
  cb_showback_db        = replace("${local.name}_showback", "-", "_")
  cb_grafana_ns         = "observability"
  cb_grafana_sa         = "kube-prometheus-stack-grafana"
  cb_showback_tables    = "s3://${aws_s3_bucket.showback.bucket}/showback/tables"
  cb_showback_first_day = "2026-11-01"

  cb_showback_columns = {
    report_by_controller = [
      ["report_date", "string"], ["controller_id", "string"], ["team_id", "string"], ["cost_centre", "string"],
      ["business_unit", "string"], ["controller_compute", "double"], ["controller_storage", "double"],
      ["agent_compute", "double"], ["shared_allocated", "double"], ["total_cost", "double"],
      ["share_pct", "double"], ["cur_included", "string"],
    ]
    showback_allocated = [
      ["report_date", "string"], ["controller_id", "string"], ["team_id", "string"], ["cost_centre", "string"],
      ["business_unit", "string"], ["resource_class", "string"], ["cpu_core_hours", "double"],
      ["ram_gib_hours", "double"], ["pv_gib_hours", "double"], ["efs_used_gib", "double"], ["cpu_cost", "double"],
      ["ram_cost", "double"], ["pv_cost", "double"], ["other_cost", "double"], ["total_cost", "double"],
      ["cur_included", "string"],
    ]
  }
}

resource "aws_glue_catalog_database" "showback" {
  name = local.cb_showback_db
}

resource "aws_glue_catalog_table" "showback" {
  for_each      = local.cb_showback_columns
  name          = each.key
  database_name = aws_glue_catalog_database.showback.name
  table_type    = "EXTERNAL_TABLE"

  parameters = {
    "classification"               = "csv"
    "skip.header.line.count"       = "1"
    "projection.enabled"           = "true"
    "projection.day.type"          = "date"
    "projection.day.format"        = "yyyy-MM-dd"
    "projection.day.range"         = "${local.cb_showback_first_day},NOW"
    "projection.day.interval"      = "1"
    "projection.day.interval.unit" = "DAYS"
    "storage.location.template"    = "${local.cb_showback_tables}/${each.key}/date=$${day}/"
  }

  partition_keys {
    name = "day"
    type = "string"
  }

  storage_descriptor {
    location      = "${local.cb_showback_tables}/${each.key}/"
    input_format  = "org.apache.hadoop.mapred.TextInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat"
    ser_de_info {
      serialization_library = "org.apache.hadoop.hive.serde2.OpenCSVSerde"
      parameters            = { "separatorChar" = ",", "quoteChar" = "\"" }
    }
    dynamic "columns" {
      for_each = each.value
      content {
        name = columns.value[0]
        type = columns.value[1]
      }
    }
  }
}

resource "aws_athena_workgroup" "showback" {
  name          = "${local.name}-showback"
  force_destroy = true
  configuration {
    result_configuration {
      output_location = "s3://${aws_s3_bucket.athena_results.bucket}/showback/"
    }
  }
  tags = local.tags
}

resource "aws_iam_role" "grafana_showback" {
  name = "${local.name}-grafana-showback"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "pods.eks.amazonaws.com" }
      Action    = ["sts:AssumeRole", "sts:TagSession"]
    }]
  })
  tags = local.tags
}

# Read-only: query the two showback tables; nothing else in the account.
resource "aws_iam_role_policy" "grafana_showback" {
  name = "showback-read"
  role = aws_iam_role.grafana_showback.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "Athena"
        Effect = "Allow"
        Action = ["athena:StartQueryExecution", "athena:GetQueryExecution", "athena:GetQueryResults", "athena:StopQueryExecution",
        "athena:GetWorkGroup", "athena:GetDataCatalog", "athena:GetDatabase", "athena:GetTableMetadata", "athena:ListDatabases", "athena:ListTableMetadata"]
        Resource = [aws_athena_workgroup.showback.arn, "arn:${data.aws_partition.current.partition}:athena:${var.aws_region}:${local.cb_account_id}:datacatalog/AwsDataCatalog"]
      },
      {
        Sid      = "AthenaList"
        Effect   = "Allow"
        Action   = ["athena:ListWorkGroups", "athena:ListDataCatalogs"]
        Resource = "*"
      },
      {
        Sid    = "Glue"
        Effect = "Allow"
        Action = ["glue:GetDatabase", "glue:GetDatabases", "glue:GetTable", "glue:GetTables", "glue:GetPartition", "glue:GetPartitions"]
        Resource = [
          "arn:${data.aws_partition.current.partition}:glue:${var.aws_region}:${local.cb_account_id}:catalog",
          "arn:${data.aws_partition.current.partition}:glue:${var.aws_region}:${local.cb_account_id}:database/${local.cb_showback_db}",
          "arn:${data.aws_partition.current.partition}:glue:${var.aws_region}:${local.cb_account_id}:table/${local.cb_showback_db}/*",
        ]
      },
      {
        Sid      = "ShowbackTablesRead"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${aws_s3_bucket.showback.arn}/showback/tables/*"
      },
      {
        Sid       = "ShowbackTablesList"
        Effect    = "Allow"
        Action    = ["s3:ListBucket", "s3:GetBucketLocation"]
        Resource  = aws_s3_bucket.showback.arn
        Condition = { StringLike = { "s3:prefix" = ["showback/tables/*"] } }
      },
      {
        Sid      = "AthenaResults"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"]
        Resource = "${aws_s3_bucket.athena_results.arn}/showback/*"
      },
      {
        Sid      = "AthenaResultsBucket"
        Effect   = "Allow"
        Action   = ["s3:ListBucket", "s3:GetBucketLocation"]
        Resource = aws_s3_bucket.athena_results.arn
      },
    ]
  })
}

resource "aws_eks_pod_identity_association" "grafana_showback" {
  cluster_name    = module.eks.cluster_name
  namespace       = local.cb_grafana_ns
  service_account = local.cb_grafana_sa
  role_arn        = aws_iam_role.grafana_showback.arn
  tags            = local.tags
}

resource "kubernetes_config_map_v1" "grafana_showback_datasource" {
  metadata {
    name      = "showback-athena-datasource"
    namespace = local.cb_grafana_ns
    labels    = { grafana_datasource = "1" }
  }
  data = {
    "showback-athena.yaml" = yamlencode({
      apiVersion = 1
      datasources = [{
        name     = "Showback (Athena)"
        uid      = "showback-athena"
        type     = "grafana-athena-datasource"
        editable = false
        jsonData = {
          authType       = "default" # AWS SDK default chain: the pod's Pod Identity credentials
          defaultRegion  = var.aws_region
          catalog        = "AwsDataCatalog"
          database       = local.cb_showback_db
          workgroup      = aws_athena_workgroup.showback.name
          outputLocation = "s3://${aws_s3_bucket.athena_results.bucket}/showback/"
        }
      }]
    })
  }
}

resource "kubernetes_config_map_v1" "grafana_showback_dashboards" {
  metadata {
    name      = "showback-dashboards"
    namespace = local.cb_grafana_ns
    labels    = { grafana_dashboard = "1" }
  }
  data = {
    "showback-platform.json" = file("${path.module}/chargeback/grafana/showback-platform.json")
    "showback-team.json"     = file("${path.module}/chargeback/grafana/showback-team.json")
  }
}

output "grafana_showback_dashboards" {
  description = "Showback dashboards in Grafana (folder General)."
  value       = ["${local.grafana_url}/d/showback-platform", "${local.grafana_url}/d/showback-team"]
}
