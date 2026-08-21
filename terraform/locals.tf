# Workspace-per-environment (docs/roadmap.md Phase 7): `terraform workspace
# select staging` / `terraform workspace select prod` picks which branch of
# env_config below applies - no separate environments/staging/, .../prod/
# directory tree, no --var-file juggling for anything sizing-related (the
# few things that do still vary per environment and aren't "how big" -
# domain_name, image tag - come in as plain -var/tfvars instead; see
# environments/*.tfvars).
#
# Deliberately no `default` key in env_config: running `terraform apply` in
# the default workspace (nobody selected staging or prod) fails the
# `local.cfg = local.env_config[local.environment]` lookup below with
# Terraform's own "Invalid index" error - a real failure, not a silent
# no-op, is exactly what should happen if nobody's chosen an environment yet.
locals {
  environment = terraform.workspace

  env_config = {
    staging = {
      vpc_cidr = "10.10.0.0/16"

      eks_node_instance_types = ["t3.medium"]
      eks_node_desired_size   = 2
      eks_node_min_size       = 1
      eks_node_max_size       = 3

      rds_instance_class      = "db.t3.medium"
      rds_multi_az            = false
      rds_deletion_protection = false

      redis_node_type    = "cache.t3.micro"
      redis_cluster_mode = false # single node

      # docs/architecture.md: "k8s-writer-role (restart, scale) — only
      # bound in staging by default" - the role is created in every
      # environment (below), but its trust policy only admits the
      # gateway's K8s ServiceAccount here.
      bind_k8s_writer_role = true

      gateway_replicas = 2
    }

    prod = {
      vpc_cidr = "10.20.0.0/16"

      eks_node_instance_types = ["t3.medium"]
      eks_node_desired_size   = 3
      eks_node_min_size       = 2
      eks_node_max_size       = 5

      rds_instance_class      = "db.r6g.large"
      rds_multi_az            = true
      rds_deletion_protection = true

      redis_node_type    = "cache.r6g.large"
      redis_cluster_mode = true # replication group with cluster mode enabled

      bind_k8s_writer_role = false

      gateway_replicas = 3
    }
  }

  cfg = local.env_config[local.environment]

  name_prefix = "mcp-control-plane-${local.environment}"

  # S3/Secrets Manager names must be globally unique per region (S3) or per
  # account (Secrets Manager, but collisions across unrelated AWS accounts
  # aren't the concern there) - account id is enough entropy for both
  # without a `random_id` resource whose value would just get thrown away
  # on every `terraform state rm`/import.
  account_id = data.aws_caller_identity.current.account_id
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
