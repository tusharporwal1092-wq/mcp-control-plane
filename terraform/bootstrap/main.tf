# Bootstraps the Terraform remote state backend itself (S3 bucket + DynamoDB
# lock table) that terraform/backend.tf then points at.
#
# Deliberately a separate, tiny root module instead of a resource inside the
# main config: the main config's `backend "s3"` block needs this bucket/table
# to already exist before `terraform init` can even run there - a config
# can't create the backend it's also relying on to store its own state
# (chicken-and-egg). This one keeps its state locally (see backend.tf below)
# since bootstrapping remote state for the thing that stores remote state
# has to stop somewhere.
#
# Run once per AWS account, before ever touching the main config:
#   cd terraform/bootstrap && terraform init && terraform apply
# Then `terraform init` in terraform/ (the main config) using the bucket/
# table names this outputs (see terraform/backend.tf and terraform/README.md).

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
  # No backend block here on purpose - this config's own state is local
  # (terraform.tfstate next to this file). It's applied once, by hand, and
  # its resources (a state bucket + lock table) are themselves the thing
  # that makes remote state possible for everything else.
}

provider "aws" {
  region = var.region
}

data "aws_caller_identity" "current" {}

locals {
  # S3 bucket names are global across *every* AWS account, not just yours -
  # a plain "mcp-control-plane-tfstate" collided with someone else's
  # existing bucket the first time this was actually run (see changes.txt),
  # which S3 reports as a confusing "wrong region" AuthorizationHeaderMalformed
  # error rather than a clear "name taken." Suffixing with your own account
  # ID (which nobody else's account can also be) makes the default
  # collision-proof instead of just "probably fine" - var.state_bucket_name
  # still overrides this entirely if you want a specific name.
  state_bucket_name = coalesce(var.state_bucket_name, "mcp-control-plane-tfstate-${data.aws_caller_identity.current.account_id}")
}

resource "aws_s3_bucket" "tf_state" {
  bucket = local.state_bucket_name

  # Never let a `terraform destroy` (of this bootstrap config, which nobody
  # should ever run against a live account) take every other environment's
  # state history with it.
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "tf_state" {
  bucket = aws_s3_bucket.tf_state.id
  versioning_configuration {
    status = "Enabled" # every state write is recoverable, not just the latest
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "tf_state" {
  bucket = aws_s3_bucket.tf_state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "aws:kms"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "tf_state" {
  bucket                  = aws_s3_bucket.tf_state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# DynamoDB (not S3's newer native `use_lockfile` locking) so this works with
# any Terraform >= 1.5, not just >= 1.10 - the CLI available when this was
# written is 1.8.x.
resource "aws_dynamodb_table" "tf_lock" {
  name         = var.lock_table_name
  billing_mode = "PAY_PER_REQUEST" # lock table traffic is bursty/tiny, not worth provisioning capacity for
  hash_key     = "LockID"

  attribute {
    name = "LockID"
    type = "S"
  }
}
