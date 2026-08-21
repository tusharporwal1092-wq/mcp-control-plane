# ALB + target group + listener (docs/roadmap.md Phase 7 Week 10: "HTTPS,
# ACM cert"). Instance-mode target group attached directly to the EKS
# managed node group's ASG, health-checking/forwarding to a fixed NodePort
# (var.gateway_node_port) the Helm release's Service also listens on
# (helm.tf) - the classic Terraform-owned-ALB pattern, chosen over
# installing the AWS Load Balancer Controller + IP-mode target groups
# because this phase's deliverable is specifically "Terraform: ALB + target
# group + listener", not "a K8s Ingress controller that happens to
# provision one" - one less moving part, one less set of IAM permissions to
# grant the controller, at the cost of load-balancing per-node rather than
# per-pod (fine at 2-3 nodes/replicas; would need the LB controller if this
# ever grew to many more pods than nodes).
resource "aws_security_group" "alb" {
  name_prefix = "${local.name_prefix}-alb-"
  vpc_id      = module.vpc.vpc_id
  description = "Public HTTPS ingress to the gateway ALB"

  ingress {
    description = "https"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    description = "http (redirected to https below)"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_lb" "gateway" {
  name               = local.name_prefix
  internal           = false
  load_balancer_type = "application"
  subnets            = module.vpc.public_subnets
  security_groups    = [aws_security_group.alb.id]

  enable_deletion_protection = local.environment == "prod"
}

resource "aws_lb_target_group" "gateway" {
  name        = local.name_prefix
  port        = var.gateway_node_port
  protocol    = "HTTP"
  vpc_id      = module.vpc.vpc_id
  target_type = "instance"

  health_check {
    path                = "/health/live" # app/main.py - no auth required, matches PUBLIC_PATHS
    protocol            = "HTTP"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    interval            = 15
    timeout             = 5
    matcher             = "200"
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.gateway.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate.gateway.arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.gateway.arn
  }
}

resource "aws_lb_listener" "http_redirect" {
  load_balancer_arn = aws_lb.gateway.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

# Registers every node in the (single) managed node group with the target
# group - ASG-attached, so nodes added/removed by the node group's own
# autoscaling (local.cfg.eks_node_min_size/max_size) register/deregister
# automatically without Terraform having to know individual instance ids.
resource "aws_autoscaling_attachment" "gateway_nodes" {
  autoscaling_group_name = module.eks.eks_managed_node_groups["default"].node_group_autoscaling_group_names[0]
  lb_target_group_arn    = aws_lb_target_group.gateway.arn
}
