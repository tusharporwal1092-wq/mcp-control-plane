# Environment-specific sizing (instance types, node counts, Multi-AZ, ...)
# lives in locals.tf's `env_config` map, keyed by `terraform.workspace` - not
# here. These are the handful of things genuinely the same across every
# environment.

variable "region" {
  description = "AWS region for every resource in this config. Left unset (null) by default rather than hardcoded to a real region: passing `region = null` to the aws provider (providers.tf) is the same as omitting the argument entirely, so the provider falls back to its normal resolution chain - AWS_REGION/AWS_DEFAULT_REGION env vars, then the region set by `aws configure` in ~/.aws/config, then EC2/ECS instance metadata. Override with -var=\"region=...\" only if you need something other than whatever your AWS CLI is already configured for."
  type        = string
  default     = null
}

variable "kubernetes_version" {
  description = "EKS control plane version."
  type        = string
  default     = "1.30"
}

variable "cluster_admin_principal_arns" {
  description = "IAM principal ARNs (users/roles) granted `AmazonEKSClusterAdminPolicy` via EKS access entries - e.g. the ARN of whoever runs `terraform apply`/`kubectl` by hand. Empty by default; without at least one entry, only the IAM principal Terraform runs as gets cluster-admin (via the eks module's `enable_cluster_creator_admin_permissions`), which is enough to bootstrap but worth setting explicitly for a real team."
  type        = list(string)
  default     = []
}

variable "domain_name" {
  description = "Domain name the ALB's ACM certificate should cover (e.g. mcp.example.com). Required for a real cert - see acm.tf for what happens when it's left unset (a plan-time error, not a silently-broken cert)."
  type        = string
  default     = ""
}

variable "route53_zone_id" {
  description = "Hosted zone ID for `domain_name`, used to auto-validate the ACM certificate via DNS. If unset, the certificate is still created but DNS validation records must be added manually (see acm.tf output)."
  type        = string
  default     = ""
}

variable "gateway_node_port" {
  description = "Fixed NodePort the gateway Service listens on (helm.tf passes this into the chart). Fixed, not auto-allocated, because alb.tf's target group has to be told a specific port to health-check/forward to at plan time - an ALB instance-mode target group and a K8s NodePort Service line up on this one number by convention, not by any API wiring between Terraform and Kubernetes."
  type        = number
  default     = 30080
}

variable "gateway_image_tag" {
  description = "Image tag (from ECR) the Helm release deploys - see helm/mcp-control-plane/values.yaml's `image.tag`."
  type        = string
  default     = "latest"
}

variable "jenkins_url" {
  description = "Jenkins base URL, wired into the gateway Deployment as JENKINS_URL - matches app/tools/tools_spec.py's env var."
  type        = string
  default     = ""
}

variable "prometheus_url" {
  description = "Prometheus base URL for read_prometheus_metrics - matches app/tools/tools_spec.py's PROMETHEUS_URL."
  type        = string
  default     = ""
}
