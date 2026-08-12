package authz

# input:  {agent: {id, role}, tool: {name, args}, resource: {namespace}, environment}
# data:   {roles: {<role>: {allowed_tools, allowed_environments, allowed_namespaces}},
#          destructive_tools, always_require_approval_tools, kube_system_blocked_tools}
# output: {allow: bool, require_approval: bool}

default allow := false
default require_approval := false

role_config := data.roles[input.agent.role]

tool_allowed if input.tool.name in role_config.allowed_tools

environment_allowed if input.environment == "unknown"

environment_allowed if input.environment in role_config.allowed_environments

namespace_allowed if input.resource.namespace == null

namespace_allowed if "*" in role_config.allowed_namespaces

namespace_allowed if input.resource.namespace in role_config.allowed_namespaces

# Hard override: some tools are never allowed against kube-system, no matter
# the role or environment (docs/tool-spec.md). Originally just
# restart_deployment; extended to exec_into_pod and apply_k8s_manifest since
# either one against kube-system is an immediate cluster-admin-equivalent
# blast radius - arbitrary code execution or arbitrary resource mutation in
# the namespace that runs the control plane itself.
kube_system_blocked if {
	input.tool.name in data.kube_system_blocked_tools
	input.resource.namespace == "kube-system"
}

allow if {
	tool_allowed
	environment_allowed
	namespace_allowed
	not kube_system_blocked
}

# Destructive-but-bounded tools (restart/scale a known deployment, trigger a
# known job): approval only in prod, since staging/dev blast radius is small
# and reversible.
require_approval if {
	allow
	input.tool.name in data.destructive_tools
	input.environment == "prod"
}

# Higher-risk tools (arbitrary command exec in a pod, arbitrary manifest
# apply): approval in *every* environment they're allowed in, not just prod -
# unlike restart_deployment/scale_deployment, these have effectively
# unbounded blast radius (exec_into_pod can run anything the pod's
# permissions allow; apply_k8s_manifest can create/mutate any namespaced
# resource, including ones that grant further privilege), so environment
# alone isn't a safe basis for skipping human review.
require_approval if {
	allow
	input.tool.name in data.always_require_approval_tools
}
