# Values that differ per environment but aren't "how big" (that's
# locals.tf's env_config, keyed by workspace instead). Apply with:
#   terraform workspace select staging   # or: terraform workspace new staging
#   terraform apply -var-file=environments/staging.tfvars

domain_name     = "staging.mcp.example.com" # placeholder - replace with a real domain you control
route53_zone_id = ""                        # set if that domain's hosted zone lives in this account, for auto DNS validation

gateway_image_tag = "latest" # staging tracks latest; prod pins a specific tag (see prod.tfvars)

jenkins_url    = "https://jenkins.staging.example.com"
prometheus_url = "https://prometheus.staging.example.com"

cluster_admin_principal_arns = []
