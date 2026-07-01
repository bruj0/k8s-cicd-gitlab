variable "cluster_name" {
  description = "Name passed to `kind create cluster --name`. Container names and kubeconfig path derive from it."
  type        = string
  default     = "cicd"
}

variable "kubernetes_version" {
  description = "kindest/node image tag. Defaults to the upstream LTS recommended for kind 0.27+."
  type        = string
  default     = "v1.31.0"
}

variable "domain" {
  description = "Base DNS domain for the *.wildcard the chart mints (read by Phase 2 gitlab installer for global.hosts.domain). Not used by Phase 1."
  type        = string
  default     = "local.bruj0.net"
}

variable "kubeconfig_path" {
  description = "Where to write the merged kubeconfig (side-by-side; we do NOT touch ~/.kube/config)."
  type        = string
  default     = "./kubeconfig"
}

variable "data_root" {
  description = "Host directory whose `shared/` sub-path is bind-mounted onto every node at /var/local/shared. Phase 2 installs a local-path StorageClass on top of it; chart-managed PVCs then create their per-PVC sub-directories under <data_root>/shared/."
  type        = string
  default     = "../data"
}

variable "node_shapes" {
  description = <<-EOT
    Ordered list of node definitions. Defaults to the spec:
      - 3 gitlab workers @ 8Gi / 4 CPU
      - 1 runner worker @ 8Gi / 4 CPU
      - 1 control-plane  @ 4Gi / 2 CPU
    Roles: 'gitlab', 'runner', or 'control-plane'. The gitlab worker
    memory bump from 4Gi → 8Gi (CPU 2 → 4) is required by GitLab chart
    10.x (GitLab 19.x) which deploys Cloud Native architecture with
    separate webservice, sidekiq, kas, gitaly, prometheus, plus the
    chart-bundled OpenBao subchart — 4Gi per worker is below GitLab's
    minimum reference architecture. With 3×8Gi + 1×8Gi + 1×4Gi, the
    cluster totals 36Gi of advisory memory which the host can comfortably
    back with the local-path StorageClass and chart-managed PVCs.
  EOT

  type = list(object({
    name   = string
    role   = string # 'gitlab' | 'runner' | 'control-plane'
    memory = string # advisory (kind does not enforce); logged in node labels
    cpu    = number # advisory
  }))

  default = [
    { name = "gitlab-1", role = "gitlab", memory = "8Gi", cpu = 4 },
    { name = "gitlab-2", role = "gitlab", memory = "8Gi", cpu = 4 },
    { name = "gitlab-3", role = "gitlab", memory = "8Gi", cpu = 4 },
    { name = "runner",   role = "runner", memory = "8Gi", cpu = 4 },
    { name = "control-plane-1", role = "control-plane", memory = "4Gi", cpu = 2 },
  ]
}

variable "preserve_stateful_data" {
  description = <<-EOT
    Bidirectional-mode flag for the host-side stateful data:

      true  → `tofu destroy` leaves `infra/data/shared/*` intact (the
              earlier "stable across recreate" contract; chart-managed
              PVCs preserved via `mv` teardown + the
              `null_resource.wipe_data` destroy hook becomes a no-op).
              Use when you want PG/Redis/MinIO/OpenBao/Gitaly state to
              survive cluster recreate (you'd need to also flip the
              local-path teardown script back to `mv` — see
              infra/scripts/bootstrap/phase2/local_path_provisioner.py
              comments).

      false → `tofu destroy` is a full reset: the cluster, every PV,
              and every host-side data dir go away. This is the
              default as of 2026-07. The chart-managed PVC dirs go via
              local-path's default `rm -rf` teardown; the host-side
              `infra/data/shared/stable/<service>` hostPath PV dirs
              are wiped by `null_resource.wipe_data`'s destroy
              provisioner.

    Set via `tofu -chdir=infra/tofu apply -var=preserve_stateful_data=true`.
    The bootstrap CLI mirrors this with `bootstrap --destroy
    --preserve-data` (see infra/scripts/bootstrap/cli.py).
  EOT

  type    = bool
  default = false
}