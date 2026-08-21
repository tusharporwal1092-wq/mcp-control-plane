# Secrets Manager secrets (docs/roadmap.md Phase 7 Week 11): DB creds,
# Redis URL, Jenkins token, Slack signing secret.
#
# DB creds aren't created here - rds.tf's `manage_master_user_password =
# true` makes RDS create and own that secret directly, specifically so its
# real password value never has to pass through (and get recorded into)
# Terraform state at all. See rds.tf's comment and this phase's exit
# criterion ("no secret appears in any Terraform state file").
#
# Two different lifecycles for what's left, not one pattern applied
# uniformly:
#   - redis_url: a value Terraform itself derives (elasticache.tf's
#     endpoint) - fully Terraform-managed, kept in sync on every apply.
#     Not actually a *secret* today - see below.
#   - jenkins_token / slack_signing_secret: values that can only come from
#     a human (a real Jenkins API token, the real Slack app's signing
#     secret) - Terraform creates the secret *container* with a placeholder
#     so downstream IAM/app wiring has a stable ARN to reference from day
#     one, then `lifecycle.ignore_changes` on the version's secret_string
#     stops `terraform apply` from ever clobbering the real value once
#     someone sets it by hand (console/CLI) - same "provisioned, not
#     populated" shape as this repo's other not-yet-configured secrets
#     (scripts/export_audit_to_s3.py's AUDIT_LOG_* env vars,
#     observability/grafana/provisioning/alerting/contact-points.yaml's
#     placeholder PagerDuty/Slack values).

resource "aws_secretsmanager_secret" "redis_url" {
  name = "mcp-control-plane/${local.environment}/redis-url"
}

resource "aws_secretsmanager_secret_version" "redis_url" {
  secret_id = aws_secretsmanager_secret.redis_url.id
  # app/redis_client.py reads REDIS_URL directly - matches its scheme/host/port shape.
  # Not actually sensitive today: elasticache.tf doesn't set an AUTH token
  # (`auth_token`), so this is host:port with no embedded credential - see
  # that file's comment for why. Still kept in Secrets Manager rather than
  # a plain ConfigMap so the app's config story stays uniform (every
  # connection string comes from the same place, whether or not it happens
  # to carry a credential today), and so adding a real auth_token later is
  # a value here, not a wholesale re-plumb.
  secret_string = jsonencode({
    redis_url = "redis://${local.redis_endpoint}:${local.redis_port}"
  })
}

resource "aws_secretsmanager_secret" "jenkins_token" {
  name = "mcp-control-plane/${local.environment}/jenkins-token"
}

resource "aws_secretsmanager_secret_version" "jenkins_token" {
  secret_id = aws_secretsmanager_secret.jenkins_token.id
  # app/tools/tools_spec.py's JENKINS_USER + JENKINS_API_TOKEN - both real
  # values only a human with Jenkins admin access can generate.
  secret_string = jsonencode({
    jenkins_user      = "REPLACE_WITH_REAL_JENKINS_USER"
    jenkins_api_token = "REPLACE_WITH_REAL_JENKINS_API_TOKEN"
  })

  lifecycle {
    ignore_changes = [secret_string]
  }
}

resource "aws_secretsmanager_secret" "slack_signing_secret" {
  name = "mcp-control-plane/${local.environment}/slack-signing-secret"
}

resource "aws_secretsmanager_secret_version" "slack_signing_secret" {
  secret_id = aws_secretsmanager_secret.slack_signing_secret.id
  # app/slack.py's SLACK_SIGNING_SECRET (HMAC verification of the
  # /admin/approvals/{id}/decide callback) + SLACK_WEBHOOK_URL (outbound
  # approval notification) - both from the real Slack app's config page.
  secret_string = jsonencode({
    slack_signing_secret = "REPLACE_WITH_REAL_SLACK_SIGNING_SECRET"
    slack_webhook_url    = "REPLACE_WITH_REAL_SLACK_WEBHOOK_URL"
  })

  lifecycle {
    ignore_changes = [secret_string]
  }
}
