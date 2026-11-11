# CHARGEBACK LAB (cur-kubecost-lab branch): daily showback export (plan test T4) and EFS usage (T3).
#
# Kubecost (EKS bundle) keeps 15 days of history. A CronJob exports one day of allocation data per
# run to S3, attributed to controllers (chargeback/showback_export.py). EFS volumes expose no
# used-bytes metric (verified in the lab), so the same pod measures each access point directory
# with du first and adds it to the export.

locals {
  cb_export_sa       = "showback-export"
  cb_export_schedule = "30 1 * * *" # 01:30 UTC, after the day closes
}

resource "aws_s3_bucket" "showback" {
  bucket        = "${local.name}-showback-${local.cb_account_id}"
  force_destroy = true
  tags          = local.tags
}

resource "aws_s3_bucket_public_access_block" "showback" {
  bucket                  = aws_s3_bucket.showback.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_iam_role" "showback_export" {
  name = "${local.name}-showback-export"
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

resource "aws_iam_role_policy" "showback_export" {
  name = "showback-write"
  role = aws_iam_role.showback_export.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["s3:PutObject"]
      Resource = "${aws_s3_bucket.showback.arn}/showback/*"
    }]
  })
}

resource "aws_eks_pod_identity_association" "showback_export" {
  cluster_name    = module.eks.cluster_name
  namespace       = local.cb_kubecost_ns
  service_account = local.cb_export_sa
  role_arn        = aws_iam_role.showback_export.arn
  tags            = local.tags
}

resource "kubernetes_service_account_v1" "showback_export" {
  metadata {
    name      = local.cb_export_sa
    namespace = local.cb_kubecost_ns
  }
  depends_on = [helm_release.kubecost]
}

resource "kubernetes_config_map_v1" "showback_export" {
  metadata {
    name      = "showback-export"
    namespace = local.cb_kubecost_ns
  }
  data = {
    "showback_export.py" = file("${path.module}/chargeback/showback_export.py")
    "mapping.csv"        = file("${path.module}/chargeback/mapping.csv")
  }
  depends_on = [helm_release.kubecost]
}

# Static, read-only mount of the EFS root: one directory per access point (/pvc-<pv name>).
resource "kubernetes_persistent_volume_v1" "showback_efs_root" {
  metadata {
    name = "showback-efs-root"
  }
  spec {
    capacity                         = { storage = "1Gi" } # nominal; EFS has no size
    access_modes                     = ["ReadWriteMany"]   # EFS CSI rejects ReadOnlyMany; the mount itself is read-only
    persistent_volume_reclaim_policy = "Retain"
    storage_class_name               = "showback-static" # static binding only; "" is dropped by the provider
    mount_options                    = ["iam"]
    persistent_volume_source {
      csi {
        driver        = "efs.csi.aws.com"
        volume_handle = module.efs.id
        read_only     = true
      }
    }
  }
}

resource "kubernetes_persistent_volume_claim_v1" "showback_efs_root" {
  metadata {
    name      = "showback-efs-root"
    namespace = local.cb_kubecost_ns
  }
  spec {
    access_modes       = ["ReadWriteMany"] # EFS CSI rejects ReadOnlyMany; the mount itself is read-only
    storage_class_name = "showback-static"
    volume_name        = kubernetes_persistent_volume_v1.showback_efs_root.metadata[0].name
    resources {
      requests = { storage = "1Gi" }
    }
  }
  depends_on = [helm_release.kubecost]
}

resource "kubernetes_cron_job_v1" "showback_export" {
  metadata {
    name      = "showback-export"
    namespace = local.cb_kubecost_ns
  }
  spec {
    schedule                      = local.cb_export_schedule
    concurrency_policy            = "Forbid"
    successful_jobs_history_limit = 3
    failed_jobs_history_limit     = 3
    job_template {
      metadata {}
      spec {
        backoff_limit = 1
        template {
          metadata {}
          spec {
            service_account_name = kubernetes_service_account_v1.showback_export.metadata[0].name
            restart_policy       = "Never"
            node_selector        = { role = "shared" }
            # EFS access point directories are owned by uid/gid 1000 with mode 700 (storage class "efs").
            security_context {
              run_as_user     = 1000
              run_as_group    = 1000
              run_as_non_root = true
            }

            init_container {
              name    = "efs-usage"
              image   = "public.ecr.aws/docker/library/python:3.13-alpine"
              command = ["/bin/sh", "-c", "du -sk /efs/pvc-* > /out/efs-usage.tsv 2>/out/efs-usage.err; cat /out/efs-usage.tsv"]
              volume_mount {
                name       = "efs-root"
                mount_path = "/efs"
                read_only  = true
              }
              volume_mount {
                name       = "out"
                mount_path = "/out"
              }
              resources {
                requests = { cpu = "50m", memory = "64Mi" }
                limits   = { memory = "256Mi" }
              }
            }

            init_container {
              name    = "export"
              image   = "public.ecr.aws/docker/library/python:3.13-alpine"
              command = ["python", "/config/showback_export.py"]
              env {
                name  = "EFS_USAGE_FILE"
                value = "/out/efs-usage.tsv"
              }
              env {
                name  = "KUBECOST_URL"
                value = "http://kubecost-frontend.${local.cb_kubecost_ns}:9090"
              }
              volume_mount {
                name       = "config"
                mount_path = "/config"
                read_only  = true
              }
              volume_mount {
                name       = "out"
                mount_path = "/out"
              }
              resources {
                requests = { cpu = "50m", memory = "128Mi" }
                limits   = { memory = "512Mi" }
              }
            }

            container {
              name    = "upload"
              image   = "public.ecr.aws/aws-cli/aws-cli:2.39.7"
              command = ["/bin/sh", "-c", "aws s3 cp /out/ \"s3://${aws_s3_bucket.showback.bucket}/showback/date=$(cat /out/day)/\" --recursive --exclude day"]
              env {
                name  = "HOME"
                value = "/tmp"
              }
              volume_mount {
                name       = "out"
                mount_path = "/out"
              }
              resources {
                requests = { cpu = "50m", memory = "128Mi" }
                limits   = { memory = "256Mi" }
              }
            }

            volume {
              name = "config"
              config_map {
                name = kubernetes_config_map_v1.showback_export.metadata[0].name
              }
            }
            volume {
              name = "out"
              empty_dir {}
            }
            volume {
              name = "efs-root"
              persistent_volume_claim {
                claim_name = kubernetes_persistent_volume_claim_v1.showback_efs_root.metadata[0].name
                read_only  = true
              }
            }
          }
        }
      }
    }
  }
  depends_on = [aws_eks_pod_identity_association.showback_export]
}

output "showback_export_run" {
  description = "Run the showback export now for a given day (UTC). Results land under showback/date=<day>/."
  value       = "kubectl create job -n ${local.cb_kubecost_ns} --from=cronjob/showback-export showback-export-manual-$(date +%s)"
}

output "showback_s3_path" {
  description = "S3 path of the daily showback files."
  value       = "s3://${aws_s3_bucket.showback.bucket}/showback/"
}
