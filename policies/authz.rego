package authz

# input:  {agent: {id, role}, tool: {name, args}, resource: {namespace}, environment}
# data:   {roles: {<role>: {allowed_tools, allowed_environments, allowed_namespaces}}, destructive_tools}
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

allow if {
	tool_allowed
	environment_allowed
	namespace_allowed
}

require_approval if {
	allow
	input.tool.name in data.destructive_tools
	input.environment == "prod"
}
