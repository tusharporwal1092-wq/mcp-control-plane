# ElastiCache Redis - backs app/redis_client.py (rate limiting sliding
# window, approval-pending state). docs/roadmap.md Phase 7 Week 10: "single
# node in staging, cluster in prod" (local.cfg.redis_cluster_mode).
#
# Both branches use aws_elasticache_replication_group (not the older
# aws_elasticache_cluster) so encryption-in-transit/at-rest and auth-token
# support are available either way - the *shape* differs (num_cache_clusters
# vs num_node_groups+replicas_per_node_group are mutually exclusive
# arguments on this resource), so it's two resources gated by `count`
# rather than one resource with conditional arguments.
#
# No `auth_token` set on either: unlike RDS (rds.tf's
# manage_master_user_password), ElastiCache has no equivalent "AWS
# generates and owns this credential, Terraform never sees it" mechanism -
# a Redis AUTH token here would mean either a `random_password` landing in
# Terraform state (the exact thing rds.tf explicitly avoids, per this
# phase's "no secret in state" exit criterion) or a human-supplied token
# with the same ignore_changes dance secrets.tf uses for Jenkins/Slack.
# Skipped for now: both security groups below already restrict access to
# "from an EKS node in this VPC" only, which is the boundary that actually
# matters for a cache with no direct internet exposure - the same
# network-isolation-over-app-level-auth tradeoff a lot of private-VPC Redis
# deployments make. Upgrade path if that stops being enough: a
# human-provided auth_token secret, same shape as jenkins_token below.
resource "aws_security_group" "redis" {
  name_prefix = "${local.name_prefix}-redis-"
  vpc_id      = module.vpc.vpc_id
  description = "Redis ingress from EKS nodes only"

  ingress {
    description     = "redis from EKS nodes"
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [module.eks.node_security_group_id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_elasticache_subnet_group" "redis" {
  name       = local.name_prefix
  subnet_ids = module.vpc.private_subnets
}

# Staging: one node, no replicas, no cluster mode - cheapest thing that
# still exercises the real rate-limiter/approval code paths.
resource "aws_elasticache_replication_group" "redis_single" {
  count = local.cfg.redis_cluster_mode ? 0 : 1

  replication_group_id = local.name_prefix
  description          = "mcp-control-plane redis (single node, ${local.environment})"

  engine         = "redis"
  engine_version = "7.1"
  node_type      = local.cfg.redis_node_type

  num_cache_clusters = 1

  subnet_group_name  = aws_elasticache_subnet_group.redis.name
  security_group_ids = [aws_security_group.redis.id]

  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
}

# Prod: cluster mode enabled - 3 shards x 1 replica each, so a single node
# failure doesn't take rate limiting/approvals down cluster-wide the way a
# single-node deployment would.
resource "aws_elasticache_replication_group" "redis_cluster" {
  count = local.cfg.redis_cluster_mode ? 1 : 0

  replication_group_id = local.name_prefix
  description          = "mcp-control-plane redis (cluster mode, ${local.environment})"

  engine         = "redis"
  engine_version = "7.1"
  node_type      = local.cfg.redis_node_type

  num_node_groups         = 3
  replicas_per_node_group = 1

  subnet_group_name  = aws_elasticache_subnet_group.redis.name
  security_group_ids = [aws_security_group.redis.id]

  at_rest_encryption_enabled = true
  transit_encryption_enabled = true

  automatic_failover_enabled = true
}

locals {
  # app/redis_client.py just needs a host:port - `configuration_endpoint_address`
  # only exists on the cluster-mode replication group (the client uses it to
  # discover shards itself); the single-node group has one cache cluster
  # whose own address is the whole story.
  redis_endpoint = local.cfg.redis_cluster_mode ? aws_elasticache_replication_group.redis_cluster[0].configuration_endpoint_address : aws_elasticache_replication_group.redis_single[0].primary_endpoint_address
  redis_port     = 6379
}
