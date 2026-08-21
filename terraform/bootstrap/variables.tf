variable "region" {
  description = "AWS region the state bucket/lock table live in. Left unset (null) by default so the aws provider (main.tf) falls back to whatever `aws configure` already set, instead of a hardcoded region that may not even be reachable from this AWS account/session (e.g. an org-level region restriction) - override with -var=\"region=...\" if you need something else."
  type        = string
  default     = null
}

variable "state_bucket_name" {
  description = "S3 bucket name for Terraform remote state. Left unset (null) by default: main.tf's local.state_bucket_name then derives \"mcp-control-plane-tfstate-<your account id>\", which - unlike a plain fixed name - can't collide with anyone else's bucket, since bucket names are global across every AWS account and account IDs aren't shared. Set this only if you want a specific name instead."
  type        = string
  default     = null
}

variable "lock_table_name" {
  description = "DynamoDB table name for Terraform state locking."
  type        = string
  default     = "mcp-control-plane-tf-lock"
}
