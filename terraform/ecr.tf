# ECR repository for the gateway image (docs/roadmap.md Phase 7 Week 10).
# One repo, shared across environments (staging/prod pull the same images
# by tag - e.g. a git sha - rather than each environment building/pushing
# its own copy), so this is deliberately *not* environment-suffixed the way
# most other resources in this config are; created once regardless of which
# workspace applies it (data-source-if-exists isn't idiomatic Terraform, so
# in practice: apply this from whichever workspace first, the other
# workspace's plan will just show no changes here).
resource "aws_ecr_repository" "gateway" {
  name                 = "mcp-control-plane/gateway"
  image_tag_mutability = "IMMUTABLE" # a tag (e.g. a git sha) always points at the same image - no silent "latest moved under you"

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "gateway" {
  repository = aws_ecr_repository.gateway.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "expire untagged images after 7 days"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 7
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "keep the last 30 tagged images"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = ["v", "sha-"]
          countType     = "imageCountMoreThan"
          countNumber   = 30
        }
        action = { type = "expire" }
      }
    ]
  })
}
