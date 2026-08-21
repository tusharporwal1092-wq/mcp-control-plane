# ACM certificate for the ALB's HTTPS listener (alb.tf). DNS-validated;
# auto-validated via Route53 when var.route53_zone_id is set, otherwise the
# certificate sits in PENDING_VALIDATION until someone adds the CNAME
# record `acm_validation_records` (outputs.tf) prints by hand.
resource "aws_acm_certificate" "gateway" {
  domain_name       = var.domain_name
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
    precondition {
      condition     = var.domain_name != ""
      error_message = "var.domain_name is required (the ALB's HTTPS listener needs a real cert) - set it via -var or environments/<env>.tfvars."
    }
  }
}

resource "aws_route53_record" "cert_validation" {
  for_each = var.route53_zone_id != "" ? {
    for dvo in aws_acm_certificate.gateway.domain_validation_options : dvo.domain_name => {
      name   = dvo.resource_record_name
      record = dvo.resource_record_value
      type   = dvo.resource_record_type
    }
  } : {}

  zone_id         = var.route53_zone_id
  name            = each.value.name
  type            = each.value.type
  ttl             = 60
  records         = [each.value.record]
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "gateway" {
  count = var.route53_zone_id != "" ? 1 : 0

  certificate_arn         = aws_acm_certificate.gateway.arn
  validation_record_fqdns = [for r in aws_route53_record.cert_validation : r.fqdn]
}
