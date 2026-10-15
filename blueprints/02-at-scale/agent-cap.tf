# TEST BRANCH (test-agent-cap): Kubernetes-native cap on agents per controller per cloud.
#
# Idea: instead of the kubernetes plugin container cap (KubernetesProvisioningLimits), every
# (controller, cloud) pair gets its own agent namespace. A ResourceQuota on that namespace is the
# cap, enforced by the API server. The plugin cap (containerCapStr) stays unset in CasC.
#
# Status: applied to a sandbox cluster (team-b only). Test results are in the engagement HOWTO.

locals {
  # One entry per (controller, cloud). Key = namespace suffix and cloud name in CasC.
  # pods: hard cap for count/pods (the "container cap" equivalent).
  # cpu/memory: optional requests ceiling, protects the node pools from a few large pods.
  agent_caps = {
    "team-b-linux" = {
      controller = "team-b"
      cloud      = "k8s-linux"
      pods       = 5
      cpu        = "5"
      memory     = "10Gi"
    }
    "team-b-linux-xl" = {
      controller = "team-b"
      cloud      = "k8s-linux-xl"
      pods       = 2
      cpu        = "4"
      memory     = "8Gi"
    }
  }

  agent_cap_ns_prefix = "${local.cbci_agents_ns}-"
}

resource "kubernetes_namespace_v1" "agent_cap" {
  for_each = local.agent_caps

  metadata {
    name = "${local.agent_cap_ns_prefix}${each.key}"
    labels = {
      "cbci/controller" = each.value.controller
      "cbci/cloud"      = each.value.cloud
    }
  }
}

resource "kubernetes_resource_quota_v1" "agent_cap" {
  for_each = local.agent_caps

  metadata {
    name      = "agent-cap"
    namespace = kubernetes_namespace_v1.agent_cap[each.key].metadata[0].name
  }

  spec {
    hard = {
      "count/pods"      = each.value.pods
      "requests.cpu"    = each.value.cpu
      "requests.memory" = each.value.memory
    }
  }
}

# ResourceQuota on requests.* rejects pods without requests, so default them (agent containers
# such as jnlp often have none). Only default_request is set on purpose: a default limit (cpu)
# lower than a template's explicit request makes the API server reject the pod as invalid
# ("requests: must be less than or equal to cpu limit"), which the plugin turns into back-off.
resource "kubernetes_limit_range_v1" "agent_cap" {
  for_each = local.agent_caps

  metadata {
    name      = "agent-defaults"
    namespace = kubernetes_namespace_v1.agent_cap[each.key].metadata[0].name
  }

  spec {
    limit {
      type = "Container"
      default_request = {
        cpu    = "100m"
        memory = "128Mi"
      }
    }
  }
}

# The controller only needs to manage agent pods in its own namespaces.
resource "kubernetes_role_v1" "agent_cap_controller" {
  for_each = local.agent_caps

  metadata {
    name      = "cbci-agent-controller"
    namespace = kubernetes_namespace_v1.agent_cap[each.key].metadata[0].name
  }

  rule {
    api_groups = [""]
    resources  = ["pods", "pods/exec"]
    verbs      = ["create", "delete", "get", "list", "patch", "update", "watch"]
  }
  rule {
    api_groups = [""]
    resources  = ["pods/log"]
    verbs      = ["get", "list", "watch"]
  }
  rule {
    api_groups = [""]
    resources  = ["events"]
    verbs      = ["list", "watch"]
  }
}

resource "kubernetes_role_binding_v1" "agent_cap_controller" {
  for_each = local.agent_caps

  metadata {
    name      = "cbci-agent-controller"
    namespace = kubernetes_namespace_v1.agent_cap[each.key].metadata[0].name
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.agent_cap_controller[each.key].metadata[0].name
  }

  # Each managed controller runs as its own ServiceAccount in the CloudBees CI namespace, named
  # after the controller (verified: team-b-0 runs as SA "team-b"). Binding only that SA keeps a
  # controller out of the namespaces of the other controllers.
  subject {
    kind      = "ServiceAccount"
    name      = each.value.controller
    namespace = module.eks_blueprints_addon_cbci.cbci_namespace
  }
}
