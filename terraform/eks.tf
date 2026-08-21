# EKS cluster + managed node group (docs/roadmap.md Phase 7 Week 10: "2-3
# nodes, t3.medium"). Uses terraform-aws-modules/eks/aws - hand-rolling an
# EKS control plane (cluster IAM role + policy attachments, OIDC provider,
# node group launch template + IAM role + policy attachments, core addons)
# is hundreds of lines of boilerplate this module already gets right and
# keeps current with AWS's own EKS API changes.
module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.31"

  cluster_name    = local.name_prefix
  cluster_version = var.kubernetes_version

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.private_subnets # nodes are private-subnet-only; the ALB (public) reaches them via NodePort/target group, not the reverse

  # IRSA (iam.tf's mcp-gateway-role/k8s-reader-role/etc. all trust this
  # cluster's OIDC provider) needs the OIDC provider the module creates by
  # default; called out explicitly since it's the one flag none of this
  # works without.
  enable_irsa = true

  # Whoever's IAM principal runs `terraform apply` gets cluster-admin via an
  # EKS access entry automatically - enough to bootstrap add-ons/Helm below
  # without a chicken-and-egg "nobody can auth to the new cluster yet"
  # problem. var.cluster_admin_principal_arns adds anyone else who needs
  # `kubectl` access day-to-day.
  enable_cluster_creator_admin_permissions = true
  access_entries = {
    for arn in var.cluster_admin_principal_arns : arn => {
      principal_arn = arn
      policy_associations = {
        admin = {
          policy_arn = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
          access_scope = {
            type = "cluster"
          }
        }
      }
    }
  }

  cluster_endpoint_public_access = true # gateway/kubectl access from outside the VPC; tighten to false + a bastion/VPN for a real prod account

  cluster_addons = {
    coredns                = {}
    kube-proxy             = {}
    vpc-cni                = {}
    eks-pod-identity-agent = {}
    # helm/mcp-control-plane's HPA (hpa.yaml) scales on CPU utilization,
    # which needs metrics-server's /apis/metrics.k8s.io API actually
    # present in the cluster - without this addon the HPA object exists
    # but permanently reports "unknown" for its current metrics.
    metrics-server = {}
  }

  eks_managed_node_groups = {
    default = {
      instance_types = local.cfg.eks_node_instance_types
      ami_type       = "AL2023_x86_64_STANDARD"

      min_size     = local.cfg.eks_node_min_size
      max_size     = local.cfg.eks_node_max_size
      desired_size = local.cfg.eks_node_desired_size

      subnet_ids = module.vpc.private_subnets
    }
  }

  # alb.tf's target group forwards to var.gateway_node_port on every node -
  # without this, the node security group (which the module otherwise locks
  # down to cluster-internal traffic only) would silently drop it.
  node_security_group_additional_rules = {
    alb_to_node_port = {
      description              = "gateway NodePort from the ALB"
      protocol                 = "tcp"
      from_port                = var.gateway_node_port
      to_port                  = var.gateway_node_port
      type                     = "ingress"
      source_security_group_id = aws_security_group.alb.id
    }
  }

  tags = {
    Environment = local.environment
  }
}

# app/main.py's gateway Deployment (helm.tf) lives here, not in `default`
# or `kube-system` - keeps RBAC/NetworkPolicy scoping to exactly this app's
# workloads, matching docs/architecture.md's deployment diagram
# ("Namespace: mcp-control-plane").
resource "kubernetes_namespace" "mcp_control_plane" {
  metadata {
    name = "mcp-control-plane"
  }

  depends_on = [module.eks]
}
