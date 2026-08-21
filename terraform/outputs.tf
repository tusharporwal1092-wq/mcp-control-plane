output "environment" {
  value = local.environment
}

output "vpc_id" {
  value = module.vpc.vpc_id
}

output "eks_cluster_name" {
  value = module.eks.cluster_name
}

output "eks_cluster_endpoint" {
  value = module.eks.cluster_endpoint
}

output "configure_kubectl" {
  description = "Run this to point kubectl at the cluster this config just created/updated."
  value       = "aws eks update-kubeconfig --name ${module.eks.cluster_name} --region ${data.aws_region.current.name}"
}

output "ecr_repository_url" {
  value = aws_ecr_repository.gateway.repository_url
}

output "rds_endpoint" {
  value     = aws_db_instance.postgres.address
  sensitive = false # hostname only, not the credential - see db_credentials_secret_arn below for the actual secret
}

output "redis_endpoint" {
  value = local.redis_endpoint
}

output "alb_dns_name" {
  description = "CNAME your DNS provider's record for var.domain_name should point at (or the Route53 alias, if managing DNS via var.route53_zone_id elsewhere)."
  value       = aws_lb.gateway.dns_name
}

output "acm_validation_records" {
  description = "DNS records to add by hand if var.route53_zone_id was left unset (otherwise Terraform already added/validated these itself)."
  value = var.route53_zone_id == "" ? {
    for dvo in aws_acm_certificate.gateway.domain_validation_options : dvo.domain_name => {
      name  = dvo.resource_record_name
      type  = dvo.resource_record_type
      value = dvo.resource_record_value
    }
  } : {}
}

output "s3_bucket_name" {
  value = aws_s3_bucket.mcp.bucket
}

output "db_credentials_secret_arn" {
  description = "RDS-owned secret (rds.tf's manage_master_user_password) - not something Terraform created or ever saw the value of."
  value       = aws_db_instance.postgres.master_user_secret[0].secret_arn
}

output "redis_url_secret_arn" {
  value = aws_secretsmanager_secret.redis_url.arn
}

output "jenkins_token_secret_arn" {
  description = "Secret exists with a placeholder value - see secrets.tf's header comment for why Terraform doesn't (can't) fill in the real Jenkins token."
  value       = aws_secretsmanager_secret.jenkins_token.arn
}

output "slack_signing_secret_arn" {
  description = "Same placeholder-value caveat as jenkins_token_secret_arn."
  value       = aws_secretsmanager_secret.slack_signing_secret.arn
}

output "irsa_role_arns" {
  value = {
    mcp_gateway = aws_iam_role.mcp_gateway.arn
    k8s_reader  = aws_iam_role.k8s_reader.arn
    k8s_writer  = aws_iam_role.k8s_writer.arn
    tfc_reader  = aws_iam_role.tfc_reader.arn
  }
}
