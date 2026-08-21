# VPC: public + private subnets, IGW, NAT GW (docs/roadmap.md Phase 7 Week
# 10). Uses the community terraform-aws-modules/vpc/aws module rather than
# hand-writing subnet/route-table/NAT wiring across 3 AZs from scratch - the
# de-facto standard module for this, actively maintained, exactly the
# "already-solved dependency" case for Terraform.
#
# Public subnets: ALB (alb.tf). Private subnets: EKS nodes, RDS,
# ElastiCache, OTel collector - matches docs/architecture.md's deployment
# diagram exactly ("Public Subnets → ALB", "Private Subnets → EKS node
# groups, RDS, Redis, OTel collector").
module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.13"

  name = local.name_prefix
  cidr = local.cfg.vpc_cidr

  azs             = slice(data.aws_availability_zones.available.names, 0, 3)
  public_subnets  = [for i in range(3) : cidrsubnet(local.cfg.vpc_cidr, 4, i)]     # .0/20, .16/20, .32/20
  private_subnets = [for i in range(3) : cidrsubnet(local.cfg.vpc_cidr, 4, i + 4)] # .64/20, .80/20, .96/20

  enable_nat_gateway = true
  # One NAT GW per AZ in prod (no single point of failure for private-subnet
  # egress); one shared NAT GW in staging (3x the cost for HA staging
  # doesn't buy anything nobody's paged for).
  single_nat_gateway     = local.environment != "prod"
  one_nat_gateway_per_az = local.environment == "prod"

  enable_dns_hostnames = true
  enable_dns_support   = true

  # Required tags for the EKS/AWS Load Balancer Controller to auto-discover
  # subnets by role (see eks.tf, alb.tf) instead of hardcoding subnet ids
  # into the ALB/target group config.
  public_subnet_tags = {
    "kubernetes.io/role/elb"                     = "1"
    "kubernetes.io/cluster/${local.name_prefix}" = "shared"
  }
  private_subnet_tags = {
    "kubernetes.io/role/internal-elb"            = "1"
    "kubernetes.io/cluster/${local.name_prefix}" = "shared"
  }
}

data "aws_availability_zones" "available" {
  state = "available"
}
