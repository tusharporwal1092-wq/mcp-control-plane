# S3 bucket for OPA policy bundles (policies/ - docs/architecture.md S3.2:
# "Policy bundles are versioned and stored in S3") and the audit log export
# (scripts/export_audit_to_s3.py writes `audit-log/<date>.ndjson` -
# docs/architecture.md S3.5: "90 days live in PostgreSQL, 7 years in S3
# Glacier after that"). One bucket, two prefixes (`opa-bundles/`,
# `audit-log/`) rather than two buckets - Object Lock/versioning/lifecycle
# below only actually need to protect the audit-log half, but Object Lock
# is a bucket-wide setting that must be enabled at creation and can never be
# turned off afterward, so splitting the bucket later (if the OPA bundle
# side ever needed different settings) is possible; merging two buckets
# into one isn't.
#
# Object Lock (Phase 7's explicit ask) is what actually makes "terraform
# destroy... audit log S3 is retained" true rather than just documented:
# WORM-locked objects can't be deleted by anyone - including a future
# `terraform destroy` - until their retention period expires, and
# `lifecycle.prevent_destroy` below additionally stops Terraform from even
# attempting to delete the *bucket* itself.
resource "aws_s3_bucket" "mcp" {
  bucket = "mcp-control-plane-${local.environment}-${local.account_id}"

  object_lock_enabled = true # must be set at creation - cannot be enabled on an existing bucket

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "mcp" {
  bucket = aws_s3_bucket.mcp.id
  versioning_configuration {
    status = "Enabled" # required for Object Lock
  }
}

resource "aws_s3_bucket_object_lock_configuration" "mcp" {
  bucket = aws_s3_bucket.mcp.id

  rule {
    default_retention {
      # COMPLIANCE: not even the account root user can shorten/remove the
      # lock before it expires - appropriate for an audit trail that has to
      # be tamper-evident (docs/architecture.md S3.5's whole point) per
      # docs/threat-model.md, not GOVERNANCE (which a sufficiently
      # privileged principal can override).
      mode  = "COMPLIANCE"
      years = 7 # matches the documented "7 years in S3 Glacier" retention policy
    }
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "mcp" {
  bucket = aws_s3_bucket.mcp.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "aws:kms"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "mcp" {
  bucket                  = aws_s3_bucket.mcp.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "mcp" {
  # Required alongside any lifecycle rule once Object Lock is on -
  # otherwise applying this can 400 with "InvalidBucketState".
  depends_on = [aws_s3_bucket_versioning.mcp]

  bucket = aws_s3_bucket.mcp.id

  rule {
    id     = "audit-log-to-glacier"
    status = "Enabled"
    filter {
      prefix = "audit-log/"
    }
    # Postgres is still the "live" 90-day copy (docs/architecture.md) - by
    # the time a row's export lands here, it's already past its live
    # window, so this transitions to Glacier almost immediately rather than
    # staging through Standard/IA first.
    transition {
      days          = 1
      storage_class = "GLACIER"
    }
    expiration {
      days = 2557 # 7 years - Object Lock's own retention (above) still governs actual deletability regardless of this
    }
    noncurrent_version_expiration {
      noncurrent_days = 2557
    }
  }

  rule {
    id     = "opa-bundles-noncurrent-cleanup"
    status = "Enabled"
    filter {
      prefix = "opa-bundles/"
    }
    # Bundles are small and versioned for rollback, not compliance -
    # unlike audit-log/, old versions just need to not accumulate forever.
    noncurrent_version_expiration {
      noncurrent_days = 90
    }
  }
}

resource "aws_s3_bucket_policy" "mcp" {
  bucket = aws_s3_bucket.mcp.id
  policy = data.aws_iam_policy_document.mcp_bucket_policy.json
}

data "aws_iam_policy_document" "mcp_bucket_policy" {
  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.mcp.arn, "${aws_s3_bucket.mcp.arn}/*"]
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}
