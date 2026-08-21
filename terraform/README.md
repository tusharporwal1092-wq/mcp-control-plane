# Terraform (docs/roadmap.md Phase 7)

100% Terraform: VPC, EKS + managed node group, ECR, RDS PostgreSQL,
ElastiCache Redis, ALB (HTTPS/ACM), IRSA IAM roles, Secrets Manager, an
Object-Locked S3 bucket, and (via `helm_release`) the app itself
(`helm/mcp-control-plane/`) + the External Secrets Operator it depends on -
one `terraform apply` reaches all of it, per this phase's stated goal.

**What's validated, and how** (see the repo's `changes.txt` for the full
account): `terraform fmt`, `terraform init` against the real public
registry, and a hand-written reference-consistency check across every
`.tf` file all pass clean. `terraform validate`/`plan`/`apply` could not be
run in the sandbox this was written in - no AWS account/credentials exist
there, and separately, a local TLS-interception layer breaks Terraform's
own internal (loopback) plugin protocol regardless of credentials, so even
a `plan` against nothing (no AWS calls yet) fails before it gets that far.
**This config has not been applied against real AWS.** Run `terraform plan`
yourself before trusting it against an account that matters, same as
you'd want for infrastructure code from anyone who hasn't run it either.

## One-time setup

1. **Bootstrap remote state** (creates the S3 bucket + DynamoDB table this
   config's own state lives in - has to exist before `terraform init` here
   can even run, see `bootstrap/main.tf`'s header comment):
   ```bash
   cd bootstrap
   terraform init
   terraform apply   # note the bucket_name/lock_table_name outputs (S3 bucket names are globally unique - you'll likely need to override the defaults)
   ```

2. **Init the main config**, pointing at whatever bootstrap actually created:
   ```bash
   cd ..
   terraform init \
     -backend-config="bucket=<state_bucket_name from step 1>" \
     -backend-config="dynamodb_table=<lock_table_name from step 1>"
   ```

3. **Pick an environment** - see `locals.tf`'s `env_config` for exactly what
   each workspace sizes differently (node counts, RDS Multi-AZ, Redis
   cluster mode, ...):
   ```bash
   terraform workspace new staging   # or: prod
   ```

4. **Set the environment-specific, non-sizing values** (domain name, image
   tag, Jenkins/Prometheus URLs) via the matching file in `environments/` -
   copy and edit, the checked-in ones are placeholders:
   ```bash
   terraform plan -var-file=environments/staging.tfvars
   terraform apply -var-file=environments/staging.tfvars
   ```

Repeat step 3-4 with `prod`/`environments/prod.tfvars` for the other
environment - same backend, same bucket/table, different state key
(the S3 backend namespaces state per workspace automatically - see
`backend.tf`'s comment).

## After apply

- `terraform output configure_kubectl` - the `aws eks update-kubeconfig`
  command to point `kubectl` at the cluster.
- Real values still need to land in the two placeholder Secrets Manager
  secrets Terraform can't generate itself (`secrets.tf`'s header comment
  explains why): `terraform output jenkins_token_secret_arn` and
  `slack_signing_secret_arn`, then e.g.
  `aws secretsmanager put-secret-value --secret-id <arn> --secret-string '{...}'`.
- If `route53_zone_id` was left unset, add the DNS records
  `terraform output acm_validation_records` prints, then point
  `domain_name`'s DNS at `terraform output alb_dns_name`.
- OPA has no real policy bundle mounted until `opa.policyConfigMapName` is
  set to a real ConfigMap (`kubectl create configmap opa-policies
  --from-file=../policies/`, then `helm upgrade` - see
  `helm/mcp-control-plane`'s `NOTES.txt`, shown automatically after
  `helm_release.mcp_control_plane` applies) - until then every policy
  check denies by default (app/authz/opa.py fails closed on anything that
  isn't a clean `allow`), which is a safe default, not a broken one.

## `terraform destroy` / rebuild - what's actually retained, and why

The explicit ask this phase makes ("can tear down and rebuild without data
loss (audit log S3 is retained)") is deliberately narrow: **only the audit
log S3 bucket** survives a `terraform destroy` of an environment. Everything
else - the cluster, RDS, Redis, the ALB - is expected to be fully
destroyable and rebuildable from this config alone; that's the whole point
of having environments be Terraform-managed rather than click-ops'd.

What makes the S3 bucket's survival a property of the config, not just a
hope:
- `aws_s3_bucket.mcp` (`s3.tf`) has `lifecycle { prevent_destroy = true }`.
  Important to get right, since it changes the runbook below: this does
  **not** mean "destroy everything else, skip just this resource" - it
  means a plan that would destroy this resource is rejected outright, so a
  bare `terraform destroy` fails immediately at the plan stage and destroys
  *nothing* until the bucket is explicitly excluded (see runbook). That's
  arguably the better safety property for an audit trail anyway: it forces
  a deliberate, explicit step to proceed at all, rather than an implicit
  "oh, it skipped that one" easy to not notice.
- Every object under `audit-log/` is additionally Object Lock
  COMPLIANCE-mode retained for 7 years
  (`aws_s3_bucket_object_lock_configuration.mcp`) - not even the account
  root user can delete a locked object before its retention expires, so
  even manually deleting the *bucket* out from under Terraform (bypassing
  Terraform entirely, `prevent_destroy` and all) still can't remove the
  audit trail early. Two independent gates, not one.

Runbook (**not yet executed against a real account** - see the caveat
above; this is what applying this config's own safety mechanisms implies
should happen, written down so it can actually be tried and corrected
against a real AWS account rather than assumed). Needs Terraform >= 1.9
for `-exclude` (the CLI available when this was written, 1.8.2, predates
it - `terraform version` first):

```bash
terraform workspace select staging

# 1. Prove the safety net first: a plain destroy must refuse to run at all.
terraform destroy -var-file=environments/staging.tfvars
# expected: fails at the plan stage with an error naming aws_s3_bucket.mcp
# ("Instance cannot be destroyed" / prevent_destroy) - nothing is deleted.

# 2. Tear down everything *except* the audit bucket and its sub-resources.
EXCLUDE="-exclude=aws_s3_bucket.mcp -exclude=aws_s3_bucket_versioning.mcp \
  -exclude=aws_s3_bucket_object_lock_configuration.mcp \
  -exclude=aws_s3_bucket_server_side_encryption_configuration.mcp \
  -exclude=aws_s3_bucket_public_access_block.mcp \
  -exclude=aws_s3_bucket_lifecycle_configuration.mcp \
  -exclude=aws_s3_bucket_policy.mcp"
terraform plan -destroy $EXCLUDE -var-file=environments/staging.tfvars   # review first
terraform destroy $EXCLUDE -var-file=environments/staging.tfvars
# expected: VPC/EKS/RDS/Redis/ALB/IAM/Secrets Manager/Helm releases are all
# destroyed; aws_s3_bucket.mcp and its sub-resources are left untouched in
# both AWS and Terraform state.

# 3. Rebuild.
terraform apply -var-file=environments/staging.tfvars
# expected: everything from step 2 is recreated from scratch;
# aws_s3_bucket.mcp is *not* recreated (`terraform plan` beforehand should
# show 0 changes for it) - it, and everything under audit-log/, survived
# the round trip untouched.
```

If a real run of this diverges from that expectation, that's a bug in this
config to fix, not a reason to skip running it before relying on it.
