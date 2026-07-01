locals {
  # Absolute host path so the kind provider (which calls docker on the host)
  # can resolve the bind mount. The Phase 2 bootstrap wires a Kubernetes
  # `local-path` StorageClass on top of this tree; chart-managed
  # PersistentVolumeClaims then get a sub-directory under <shared_host_abs>
  # per PVC. Host port 80/443 are reserved on the control-plane only.
  shared_host_abs = abspath("${var.data_root}/shared")

  # Resolve each node shape into a kind-style node spec.
  nodes = [
    for n in var.node_shapes : merge(n, {
      role_kind = n.role == "control-plane" ? "control-plane" : "worker"
      labels = concat(
        ["node.kubernetes.io/role=${n.role}"],
        n.role == "control-plane" ? ["ingress-ready=true"] : [],
      )
    })
  ]
}