# Remote state in the bucket/table terraform/bootstrap/ creates (run that
# once, first - see terraform/README.md). `bucket`/`dynamodb_table` below
# match bootstrap/variables.tf's defaults; since S3 bucket names are
# globally unique across every AWS account, real usage almost always
# overrides these at init time rather than editing this file:
#
#   terraform init \
#     -backend-config="bucket=<your-actual-state-bucket>" \
#     -backend-config="dynamodb_table=<your-actual-lock-table>"
#
# No `region` set here on purpose (same reasoning as variables.tf's
# `region` variable defaulting to null): a backend block can't reference
# variables at all - it's evaluated before the rest of the config, so
# there's no `var.region` to point at even if we wanted to - but the S3
# backend has its own fallback chain when `region` is omitted, identical to
# the aws provider's (AWS_REGION/AWS_DEFAULT_REGION env vars, then whatever
# `aws configure` wrote to ~/.aws/config). A hardcoded region here bit
# exactly this way already once (state-bucket creation failed against an
# account whose actual region didn't match) - see changes.txt. Override
# with `-backend-config="region=..."` at init time if you ever need state
# to live in a different region than everything else this config creates.
#
# Workspace-per-environment (staging / prod - see locals.tf) needs no extra
# config here: the S3 backend already namespaces state per workspace under
# `env:/<workspace>/<key>` automatically, so `staging` and `prod` never
# share a state file even though they share this one backend block.
terraform {
  backend "s3" {
    bucket         = "mcp-control-plane-tfstate"
    key            = "mcp-control-plane/terraform.tfstate"
    dynamodb_table = "mcp-control-plane-tf-lock"
    encrypt        = true
  }
}
