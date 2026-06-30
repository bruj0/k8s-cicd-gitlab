locals {
  # Absolute host paths so the kind provider (which calls docker on the host) can resolve them.
  data_root_abs   = abspath(var.data_root)
  shared_host_abs = abspath("${var.data_root}/shared")

  # Resolve each node shape into a kind-style node spec.
  nodes = [
    for n in var.node_shapes : merge(n, {
      role_kind = n.role == "control-plane" ? "control-plane" : "worker"
      labels = concat(
        ["node.kubernetes.io/role=${n.role}"],
        n.role == "control-plane" ? ["ingress-ready=true"] : [],
      )
      node_mount_host = n.role == "control-plane" ? null : abspath("${var.data_root}/node${n.node_index}")
      node_mount_path = n.role == "control-plane" ? null : "/var/local/node${n.node_index}"
    })
  ]
}