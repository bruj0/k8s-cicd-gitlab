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
      - 3 gitlab workers @ 4Gi / 2 CPU
      - 1 runner worker @ 8Gi / 4 CPU
      - 1 control-plane  @ 4Gi / 2 CPU
    Roles: 'gitlab', 'runner', or 'control-plane'.
  EOT

  type = list(object({
    name   = string
    role   = string # 'gitlab' | 'runner' | 'control-plane'
    memory = string # advisory (kind does not enforce); logged in node labels
    cpu    = number # advisory
  }))

  default = [
    { name = "gitlab-1", role = "gitlab", memory = "4Gi", cpu = 2 },
    { name = "gitlab-2", role = "gitlab", memory = "4Gi", cpu = 2 },
    { name = "gitlab-3", role = "gitlab", memory = "4Gi", cpu = 2 },
    { name = "runner",   role = "runner", memory = "8Gi", cpu = 4 },
    { name = "control-plane-1", role = "control-plane", memory = "4Gi", cpu = 2 },
  ]
}