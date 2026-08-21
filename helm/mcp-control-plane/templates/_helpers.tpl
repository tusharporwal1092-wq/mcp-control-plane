{{- define "mcp-control-plane.name" -}}
mcp-gateway
{{- end -}}

{{- define "mcp-control-plane.labels" -}}
app.kubernetes.io/name: {{ include "mcp-control-plane.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end -}}

{{- define "mcp-control-plane.selectorLabels" -}}
app.kubernetes.io/name: {{ include "mcp-control-plane.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
