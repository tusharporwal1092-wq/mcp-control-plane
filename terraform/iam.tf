# IRSA roles (docs/roadmap.md Phase 7 Week 11 / docs/architecture.md's
# deployment diagram: "IAM IRSA Roles (per tool executor)").
#
# Honest caveat this config can't paper over: IRSA is one IAM role per K8s
# ServiceAccount, and a Pod has exactly one ServiceAccount. app/main.py is a
# single FastAPI process handling every tool (K8s, Terraform, Jenkins,
# Prometheus, tickets) in one Deployment (helm.tf) - so only
# `mcp-gateway-role`, bound to that Deployment's actual ServiceAccount, is
# something the running app can assume today. `k8s-reader-role`,
# `k8s-writer-role`, and `tfc-reader-role` are provisioned exactly as
# docs/architecture.md's "per tool executor" design calls for - each with
# its own ServiceAccount (k8s-rbac.tf) ready to bind to a *separate*
# Deployment - but nothing in this codebase runs as that separate
# Deployment yet, so they sit unused until the gateway is actually split
# per-executor. Provisioning them now means that split doesn't also need an
# IAM change later.
locals {
  oidc_provider_arn = module.eks.oidc_provider_arn
  oidc_provider     = module.eks.oidc_provider # host/path only, no scheme - what the trust policy's Condition key needs

  irsa_namespace = "mcp-control-plane"
}

# ---------------------------------------------------------------------------
# mcp-gateway-role - the one actually bound to the running gateway pod.
# Secrets Manager read (secrets.tf's 4 secrets) + S3 (s3.tf: read OPA
# bundles, write audit export) - matches docs/architecture.md's
# "mcp-gateway-role (Secrets Manager, S3)" exactly.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "mcp_gateway_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.oidc_provider}:sub"
      values   = ["system:serviceaccount:${local.irsa_namespace}:mcp-gateway"]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_provider}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "mcp_gateway" {
  name               = "${local.name_prefix}-mcp-gateway-role"
  assume_role_policy = data.aws_iam_policy_document.mcp_gateway_trust.json
}

data "aws_iam_policy_document" "mcp_gateway_permissions" {
  statement {
    sid     = "SecretsRead"
    effect  = "Allow"
    actions = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
    resources = [
      aws_db_instance.postgres.master_user_secret[0].secret_arn, # RDS-owned - see rds.tf's comment
      aws_secretsmanager_secret.redis_url.arn,
      aws_secretsmanager_secret.jenkins_token.arn,
      aws_secretsmanager_secret.slack_signing_secret.arn,
    ]
  }
  statement {
    sid       = "OpaBundleRead"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:ListBucket"]
    resources = [aws_s3_bucket.mcp.arn, "${aws_s3_bucket.mcp.arn}/opa-bundles/*"]
  }
  statement {
    # Only meaningful once/if scripts/export_audit_to_s3.py's job moves
    # in-cluster (a CronJob using this pod's identity) instead of running as
    # the separate GitHub Actions workflow it is today
    # (.github/workflows/audit-export.yml, its own AWS creds via secrets) -
    # granted now so that move doesn't also need an IAM change.
    sid       = "AuditExportWrite"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.mcp.arn}/audit-log/*"]
  }
}

resource "aws_iam_role_policy" "mcp_gateway" {
  name   = "permissions"
  role   = aws_iam_role.mcp_gateway.id
  policy = data.aws_iam_policy_document.mcp_gateway_permissions.json
}

# ---------------------------------------------------------------------------
# k8s-reader-role / k8s-writer-role - docs/architecture.md: "k8s-reader-role
# (get pods, logs)", "k8s-writer-role (restart, scale) — only bound in
# staging by default".
#
# The IAM policies below are deliberately thin (just eks:DescribeCluster,
# a harmless self-identification call) and NOT where "reader" vs "writer"
# is actually enforced: a pod authenticates to its *own* cluster's API
# server using its ServiceAccount's mounted token, validated by the API
# server locally - that's plain Kubernetes auth, not an AWS API call, so no
# IAM permission is involved in it at all (unlike mcp-gateway-role above,
# whose Secrets Manager/S3 calls really are AWS API calls IRSA has to
# authorize). The real "get pods, logs" / "restart, scale" boundary is
# Kubernetes RBAC, bound to these same ServiceAccount names in
# k8s-rbac.tf. These two IAM roles exist so each hypothetical
# per-executor pod (see this file's header comment) still gets its own
# distinct AWS identity/CloudTrail trail, matching docs/architecture.md's
# "IRSA role: k8s-reader-role/k8s-writer-role" naming, without pretending
# IAM is the mechanism enforcing K8s-level access.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "k8s_reader_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_provider}:sub"
      values   = ["system:serviceaccount:${local.irsa_namespace}:k8s-reader"]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_provider}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "k8s_reader" {
  name               = "${local.name_prefix}-k8s-reader-role"
  assume_role_policy = data.aws_iam_policy_document.k8s_reader_trust.json
}

data "aws_iam_policy_document" "k8s_reader_permissions" {
  statement {
    sid       = "EksApiAuth"
    effect    = "Allow"
    actions   = ["eks:DescribeCluster"]
    resources = [module.eks.cluster_arn]
  }
}

resource "aws_iam_role_policy" "k8s_reader" {
  name   = "permissions"
  role   = aws_iam_role.k8s_reader.id
  policy = data.aws_iam_policy_document.k8s_reader_permissions.json
}

data "aws_iam_policy_document" "k8s_writer_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_provider}:sub"
      values   = ["system:serviceaccount:${local.irsa_namespace}:k8s-writer"]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_provider}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "k8s_writer" {
  name               = "${local.name_prefix}-k8s-writer-role"
  assume_role_policy = data.aws_iam_policy_document.k8s_writer_trust.json
}

data "aws_iam_policy_document" "k8s_writer_permissions" {
  statement {
    sid       = "EksApiAuth"
    effect    = "Allow"
    actions   = ["eks:DescribeCluster"]
    resources = [module.eks.cluster_arn]
  }
}

resource "aws_iam_role_policy" "k8s_writer" {
  name   = "permissions"
  role   = aws_iam_role.k8s_writer.id
  policy = data.aws_iam_policy_document.k8s_writer_permissions.json
}

# ---------------------------------------------------------------------------
# tfc-reader-role - docs/architecture.md: "tfc-reader-role (Terraform Cloud
# read token)". Scoped by the *naming convention* secrets.tf's other
# secrets use, not a concrete secret ARN - a Terraform Cloud token secret
# isn't one of this phase's four named Secrets Manager secrets (DB creds,
# Redis URL, Jenkins token, Slack signing secret), so provisioning it is
# left for whenever query_terraform_plan (app/tools/tools_spec.py, reads
# TFC_TOKEN from plain env today) actually needs one - adding that secret
# later needs no IAM change here.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "tfc_reader_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_provider}:sub"
      values   = ["system:serviceaccount:${local.irsa_namespace}:tfc-reader"]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_provider}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "tfc_reader" {
  name               = "${local.name_prefix}-tfc-reader-role"
  assume_role_policy = data.aws_iam_policy_document.tfc_reader_trust.json
}

data "aws_iam_policy_document" "tfc_reader_permissions" {
  statement {
    sid       = "FutureTfcTokenSecretRead"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
    resources = ["arn:aws:secretsmanager:${data.aws_region.current.name}:${local.account_id}:secret:mcp-control-plane/${local.environment}/*"]
  }
}

resource "aws_iam_role_policy" "tfc_reader" {
  name   = "permissions"
  role   = aws_iam_role.tfc_reader.id
  policy = data.aws_iam_policy_document.tfc_reader_permissions.json
}
