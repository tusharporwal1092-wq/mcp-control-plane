# Drives the Kubernetes-manifests half of this phase (helm/mcp-control-plane/)
# through the same `terraform apply` as the AWS infra above, per this
# phase's goal: "Must be reproducible from scratch with a single
# `terraform apply`" - a chart nobody ever actually installs isn't that.
#
# External Secrets Operator first: helm/mcp-control-plane's ExternalSecret/
# SecretStore objects (templates/external-secret.yaml) need its CRDs to
# exist before Helm can even validate them, so this has to land before the
# app release below, not just "eventually."
resource "helm_release" "external_secrets" {
  name             = "external-secrets"
  repository       = "https://charts.external-secrets.io"
  chart            = "external-secrets"
  namespace        = "external-secrets-system"
  create_namespace = true
  version          = "0.10.4"

  depends_on = [module.eks]
}

resource "helm_release" "mcp_control_plane" {
  name      = "mcp-control-plane"
  chart     = "${path.module}/../helm/mcp-control-plane"
  namespace = kubernetes_namespace.mcp_control_plane.metadata[0].name

  values = [
    yamlencode({
      namespace   = local.irsa_namespace
      environment = local.environment
      awsRegion   = data.aws_region.current.name

      image = {
        repository = aws_ecr_repository.gateway.repository_url
        tag        = var.gateway_image_tag
      }

      replicaCount = local.cfg.gateway_replicas

      service = {
        type     = "NodePort"
        port     = 8000
        nodePort = var.gateway_node_port
      }

      serviceAccount = {
        create = false
        name   = kubernetes_service_account.mcp_gateway.metadata[0].name
      }

      hpa = {
        enabled     = true
        minReplicas = local.cfg.gateway_replicas
        maxReplicas = local.cfg.gateway_replicas * 3
      }

      env = {
        jenkinsUrl    = var.jenkins_url
        prometheusUrl = var.prometheus_url
      }

      # None of these three are secret values - the RDS host/port/dbname
      # are just addressing info, and dbSecretArn is a pointer *to* the
      # credential (which ESO reads directly from Secrets Manager into the
      # cluster, per templates/external-secret.yaml), never the credential
      # itself. Safe to pass as plain Helm values / show up in this
      # resource's own state, same as alb_dns_name or any other
      # non-sensitive output.
      dbSecretArn = aws_db_instance.postgres.master_user_secret[0].secret_arn
      rds = {
        host   = aws_db_instance.postgres.address
        port   = aws_db_instance.postgres.port
        dbName = aws_db_instance.postgres.db_name
      }
    })
  ]

  depends_on = [
    module.eks,
    helm_release.external_secrets,
    kubernetes_service_account.mcp_gateway,
    aws_iam_role_policy.mcp_gateway,
  ]
}
