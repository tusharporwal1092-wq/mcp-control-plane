# Apply with:
#   terraform workspace select prod   # or: terraform workspace new prod
#   terraform apply -var-file=environments/prod.tfvars

domain_name     = "mcp.example.com" # placeholder - replace with a real domain you control
route53_zone_id = ""

# Prod pins an exact, already-tested image tag (e.g. a git sha or release
# tag pushed by CI) rather than "latest" - staging is where "latest" gets
# tried first.
gateway_image_tag = "REPLACE_WITH_A_REAL_TAG"

jenkins_url    = "https://jenkins.example.com"
prometheus_url = "https://prometheus.example.com"

cluster_admin_principal_arns = []
