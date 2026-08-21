# The Kubernetes-side half of iam.tf's IRSA roles: one ServiceAccount per
# role (the `eks.amazonaws.com/role-arn` annotation is what actually wires
# IRSA up - without it, the IAM role's trust policy has nothing to bind to)
# plus, for k8s-reader/k8s-writer, the RBAC Role/RoleBinding that's the
# *real* "get pods, logs" / "restart, scale" boundary (see iam.tf's comment
# on why that's RBAC and not IAM).

resource "kubernetes_service_account" "mcp_gateway" {
  metadata {
    name      = "mcp-gateway"
    namespace = kubernetes_namespace.mcp_control_plane.metadata[0].name
    annotations = {
      "eks.amazonaws.com/role-arn" = aws_iam_role.mcp_gateway.arn
    }
  }
}

resource "kubernetes_service_account" "k8s_reader" {
  metadata {
    name      = "k8s-reader"
    namespace = kubernetes_namespace.mcp_control_plane.metadata[0].name
    annotations = {
      "eks.amazonaws.com/role-arn" = aws_iam_role.k8s_reader.arn
    }
  }
}

resource "kubernetes_role" "k8s_reader" {
  metadata {
    name      = "k8s-reader"
    namespace = kubernetes_namespace.mcp_control_plane.metadata[0].name
  }

  rule {
    api_groups = [""]
    resources  = ["pods", "pods/log"]
    verbs      = ["get", "list"]
  }
  rule {
    api_groups = ["apps"]
    resources  = ["deployments"]
    verbs      = ["get", "list"]
  }
}

resource "kubernetes_role_binding" "k8s_reader" {
  metadata {
    name      = "k8s-reader"
    namespace = kubernetes_namespace.mcp_control_plane.metadata[0].name
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role.k8s_reader.metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account.k8s_reader.metadata[0].name
    namespace = kubernetes_namespace.mcp_control_plane.metadata[0].name
  }
}

# docs/architecture.md: "k8s-writer-role (restart, scale) — only bound in
# staging by default" - `count` gates the ServiceAccount (and therefore the
# whole IRSA binding) on local.cfg.bind_k8s_writer_role, so in prod the IAM
# role exists (iam.tf) but nothing can actually assume it: no
# ServiceAccount carries its role-arn annotation, and the RoleBinding below
# doesn't exist either. This is the enforcement point for that "only in
# staging" rule, not a comment somewhere hoping someone remembers it.
resource "kubernetes_service_account" "k8s_writer" {
  count = local.cfg.bind_k8s_writer_role ? 1 : 0

  metadata {
    name      = "k8s-writer"
    namespace = kubernetes_namespace.mcp_control_plane.metadata[0].name
    annotations = {
      "eks.amazonaws.com/role-arn" = aws_iam_role.k8s_writer.arn
    }
  }
}

resource "kubernetes_role" "k8s_writer" {
  count = local.cfg.bind_k8s_writer_role ? 1 : 0

  metadata {
    name      = "k8s-writer"
    namespace = kubernetes_namespace.mcp_control_plane.metadata[0].name
  }

  rule {
    api_groups = ["apps"]
    resources  = ["deployments"]
    verbs      = ["get", "list"]
  }
  rule {
    api_groups = ["apps"]
    resources  = ["deployments/scale"]
    verbs      = ["get", "update", "patch"]
  }
  rule {
    # `kubectl rollout restart` (what restart_deployment - app/tools/tools_spec.py
    # - actually does under the hood) patches the pod template's restart
    # annotation, i.e. a `patch` on the deployment itself, not a separate verb.
    api_groups = ["apps"]
    resources  = ["deployments"]
    verbs      = ["patch"]
  }
}

resource "kubernetes_role_binding" "k8s_writer" {
  count = local.cfg.bind_k8s_writer_role ? 1 : 0

  metadata {
    name      = "k8s-writer"
    namespace = kubernetes_namespace.mcp_control_plane.metadata[0].name
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role.k8s_writer[0].metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account.k8s_writer[0].metadata[0].name
    namespace = kubernetes_namespace.mcp_control_plane.metadata[0].name
  }
}

resource "kubernetes_service_account" "tfc_reader" {
  metadata {
    name      = "tfc-reader"
    namespace = kubernetes_namespace.mcp_control_plane.metadata[0].name
    annotations = {
      "eks.amazonaws.com/role-arn" = aws_iam_role.tfc_reader.arn
    }
  }
}
