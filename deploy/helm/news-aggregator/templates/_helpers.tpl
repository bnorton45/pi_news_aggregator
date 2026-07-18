{{/* Component image ref: digest pin when set (PLAN §3.4), tag fallback otherwise.
     Usage: {{ include "na.image" (list "usgs" .Values) }} */}}
{{- define "na.image" -}}
{{- $name := index . 0 -}}
{{- $values := index . 1 -}}
{{- $digest := index $values.images.digests $name -}}
{{- if $digest -}}
{{ printf "%s/%s@%s" $values.images.registry $name $digest }}
{{- else -}}
{{ printf "%s/%s:%s" $values.images.registry $name $values.images.tag }}
{{- end -}}
{{- end }}

{{/* Container hardening common to every workload (PLAN §3.2 / PSS restricted). */}}
{{- define "na.containerSecurity" -}}
securityContext:
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  capabilities:
    drop: ["ALL"]
{{- end }}

{{/* Pod-level security: pass the runAsUser uid as the sole argument. */}}
{{- define "na.podSecurity" -}}
securityContext:
  runAsNonRoot: true
  runAsUser: {{ . }}
  runAsGroup: {{ . }}
  fsGroup: {{ . }}
  seccompProfile:
    type: RuntimeDefault
{{- end }}

{{/* Soft placement preference (PLAN §2): "state" or "compute". */}}
{{- define "na.nodeAffinity" -}}
{{- $role := index . 0 -}}
{{- $values := index . 1 -}}
{{- if $values.topology.enabled }}
affinity:
  nodeAffinity:
    preferredDuringSchedulingIgnoredDuringExecution:
      - weight: 90
        preference:
          matchExpressions:
            - { key: role, operator: In, values: [{{ $role }}] }
{{- end }}
{{- end }}

{{/* Image pull secret block (empty in dev). */}}
{{- define "na.pullSecrets" -}}
{{- if .Values.imagePullSecret }}
imagePullSecrets:
  - name: {{ .Values.imagePullSecret }}
{{- end }}
{{- end }}
