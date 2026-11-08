{{- define "coa.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Kept to 40 chars so component suffixes and StatefulSet revision labels stay under 63. */}}
{{- define "coa.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 40 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 40 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 40 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "coa.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
app.kubernetes.io/name: {{ include "coa.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "coa.selectorLabels" -}}
app.kubernetes.io/name: {{ include "coa.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "coa.image" -}}
{{- $root := index . 0 -}}
{{- $img := index . 1 -}}
{{- $tag := default $root.Chart.AppVersion $img.tag -}}
{{- if $root.Values.global.imageRegistry -}}
{{- printf "%s/%s:%s" $root.Values.global.imageRegistry $img.repository $tag -}}
{{- else -}}
{{- printf "%s:%s" $img.repository $tag -}}
{{- end -}}
{{- end -}}

{{- define "coa.secretName" -}}
{{- default (printf "%s-secrets" (include "coa.fullname" .)) .Values.secrets.existingSecret -}}
{{- end -}}

{{- define "coa.backendService" -}}{{ include "coa.fullname" . }}-backend{{- end -}}
{{- define "coa.frontendService" -}}{{ include "coa.fullname" . }}-frontend{{- end -}}
{{- define "coa.llmService" -}}{{ include "coa.fullname" . }}-llm{{- end -}}
{{- define "coa.memoryService" -}}{{ include "coa.fullname" . }}-memory{{- end -}}
{{- define "coa.frontendTlsSecret" -}}
{{- default (printf "%s-frontend-tls" (include "coa.fullname" .)) .Values.frontend.tls.existingSecret -}}
{{- end -}}
{{/* Port the browser reaches the UI on: used for the http->https redirect. */}}
{{- define "coa.frontendHttpsPort" -}}{{ .Values.frontend.service.port }}{{- end -}}

{{- define "coa.llmUrl" -}}
{{- if .Values.llm.externalUrl -}}
{{- .Values.llm.externalUrl -}}
{{- else -}}
{{- printf "http://%s:11434" (include "coa.llmService" .) -}}
{{- end -}}
{{- end -}}

{{- define "coa.memoryConnectionString" -}}
{{- if .Values.memory.bundled.enabled -}}
{{- printf "couchbase://%s" (include "coa.memoryService" .) -}}
{{- else -}}
{{- required "memory.external.connectionString is required when memory.bundled.enabled=false" .Values.memory.external.connectionString -}}
{{- end -}}
{{- end -}}

{{/*
Read a key back from the release's existing Secret so generated values stay
stable across upgrades; otherwise use the provided value or generate one.
*/}}
{{- define "coa.secretValue" -}}
{{- $root := index . 0 -}}
{{- $key := index . 1 -}}
{{- $provided := index . 2 -}}
{{- $generator := index . 3 -}}
{{- $existing := lookup "v1" "Secret" $root.Release.Namespace (printf "%s-secrets" (include "coa.fullname" $root)) -}}
{{- if $provided -}}
{{- $provided -}}
{{- else if and $existing (hasKey $existing.data $key) -}}
{{- index $existing.data $key | b64dec -}}
{{- else if eq $generator "fernet" -}}
{{- randAlphaNum 32 | b64enc | replace "+" "-" | replace "/" "_" -}}
{{- else if eq $generator "token" -}}
{{- randAlphaNum 48 -}}
{{- end -}}
{{- end -}}

{{- define "coa.backendServiceAccount" -}}{{ include "coa.fullname" . }}-backend{{- end -}}
{{- define "coa.tlsManagedByApp" -}}
{{- if and .Values.frontend.tls.enabled .Values.frontend.tls.managedByApp }}true{{ end -}}
{{- end -}}
