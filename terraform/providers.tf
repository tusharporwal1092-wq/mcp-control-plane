provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project     = "mcp-control-plane"
      Environment = local.environment
      ManagedBy   = "terraform"
    }
  }
}

# Both providers below authenticate to the cluster this same config creates
# (module.eks), via a short-lived token from `aws eks get-token` rather than
# a long-lived kubeconfig credential - standard pattern for a single
# `terraform apply` to both stand up EKS and then deploy into it.
provider "kubernetes" {
  host                   = module.eks.cluster_endpoint
  cluster_ca_certificate = base64decode(module.eks.cluster_certificate_authority_data)
  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = ["eks", "get-token", "--cluster-name", module.eks.cluster_name, "--region", data.aws_region.current.name]
  }
}

provider "helm" {
  kubernetes {
    host                   = module.eks.cluster_endpoint
    cluster_ca_certificate = base64decode(module.eks.cluster_certificate_authority_data)
    exec {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "aws"
      args        = ["eks", "get-token", "--cluster-name", module.eks.cluster_name, "--region", data.aws_region.current.name]
    }
  }
}
