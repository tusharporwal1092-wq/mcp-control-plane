package authz_test

import data.authz

# Kept in sync with app/tools/tools_spec.py's TOOLS registry.
all_tools := {
	"get_pod_logs", "list_pods", "get_deployment_status",
	"restart_deployment", "scale_deployment", "query_terraform_plan",
	"trigger_jenkins_job", "get_jenkins_job_status", "read_prometheus_metrics",
	"open_ticket", "read_ticket",
}

all_environments := {"dev", "staging", "prod"}

decision(role, tool, env, namespace) := result if {
	result := authz.allow with input as {
		"agent": {"role": role},
		"tool": {"name": tool, "args": {}},
		"resource": {"namespace": namespace},
		"environment": env,
	}
}

requires_approval(role, tool, env, namespace) := result if {
	result := authz.require_approval with input as {
		"agent": {"role": role},
		"tool": {"name": tool, "args": {}},
		"resource": {"namespace": namespace},
		"environment": env,
	}
}

# --- Full matrix: every role x every tool x every environment -------------

test_every_role_allowed_its_tools_in_its_environments if {
	every role, cfg in data.roles {
		every tool in cfg.allowed_tools {
			every env in cfg.allowed_environments {
				decision(role, tool, env, null)
			}
		}
	}
}

test_every_role_denied_tools_outside_its_allowlist if {
	every role, cfg in data.roles {
		allowed := {t | some t in cfg.allowed_tools}
		every tool in (all_tools - allowed) {
			every env in all_environments {
				not decision(role, tool, env, null)
			}
		}
	}
}

test_every_role_denied_environments_outside_its_allowlist if {
	every role, cfg in data.roles {
		allowed_envs := {e | some e in cfg.allowed_environments}
		every env in (all_environments - allowed_envs) {
			every tool in cfg.allowed_tools {
				not decision(role, tool, env, null)
			}
		}
	}
}

test_unknown_role_denied_every_tool_and_environment if {
	every tool in all_tools {
		every env in all_environments {
			not decision("no-such-role", tool, env, null)
		}
	}
}

# --- Documented scenarios (docs/tool-spec.md, docs/api-design.md) ---------

test_sre_get_pod_logs_allowed_in_prod if {
	decision("sre1", "get_pod_logs", "prod", "prod-payments")
}

test_sre_restart_deployment_in_prod_requires_approval if {
	decision("sre1", "restart_deployment", "prod", "prod-payments")
	requires_approval("sre1", "restart_deployment", "prod", "prod-payments")
}

test_restart_deployment_not_require_approval_outside_prod if {
	decision("sre1", "restart_deployment", "staging", "staging-payments")
	not requires_approval("sre1", "restart_deployment", "staging", "staging-payments")
}

test_readonly_cannot_restart_deployment_in_prod if {
	not decision("readonly", "restart_deployment", "prod", "prod-payments")
}

test_deploy_bot_denied_in_prod if {
	not decision("deploy-bot", "restart_deployment", "prod", "prod-payments")
}

test_deploy_bot_allowed_in_staging if {
	decision("deploy-bot", "restart_deployment", "staging", "staging-payments")
}

test_restart_deployment_blocked_in_kube_system_regardless_of_role if {
	not decision("sre1", "restart_deployment", "staging", "kube-system")
	not decision("deploy-bot", "restart_deployment", "staging", "kube-system")
}

test_tool_without_namespace_bypasses_environment_check if {
	decision("readonly", "read_ticket", "unknown", null)
}
