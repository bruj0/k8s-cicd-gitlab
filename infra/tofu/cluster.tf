###############################################################################
# Phase 1 cluster: 1 control-plane + 4 workers, per-node hostPath bind mounts,
# shared bind mount on every node, host port 80/443 reserved for Phase 2 Traefik.
###############################################################################

resource "kind_cluster" "cicd" {
  name            = var.cluster_name
  node_image      = "kindest/node:${var.kubernetes_version}"
  wait_for_ready  = true
  kubeconfig_path = abspath(var.kubeconfig_path)

  kind_config {
    kind        = "Cluster"
    api_version = "kind.x-k8s.io/v1alpha4"

    dynamic "node" {
      for_each = local.nodes
      content {
        role = node.value.role_kind

        # Per-node extraMounts (control-plane skipped — it owns no data slot).
        dynamic "extra_mounts" {
          for_each = node.value.node_mount_host == null ? [] : [node.value.node_mount_host]
          content {
            host_path      = extra_mounts.value
            container_path = node.value.node_mount_path
            read_only      = false
            propagation    = "Bidirectional"
          }
        }

        # Shared extraMount on EVERY node, including control-plane.
        extra_mounts {
          host_path      = local.shared_host_abs
          container_path = "/var/local/shared"
          read_only      = false
          propagation    = "Bidirectional"
        }

        labels = {
          for l in node.value.labels : split("=", l)[0] => split("=", l)[1]
        }

        # Only the control-plane reserves host ports 80/443 for the Phase 2
        # Traefik entrypoint. Putting the mapping on every node would make
        # docker reject the second container that tries to claim 127.0.0.1:80.
        # TODO(phase-2): reassign these mappings if Traefik ends up on a
        # dedicated node.
        dynamic "extra_port_mappings" {
          for_each = node.value.role_kind == "control-plane" ? [1] : []
          content {
            container_port = 80
            host_port      = 80
            protocol       = "TCP"
            listen_address = "127.0.0.1"
          }
        }
        dynamic "extra_port_mappings" {
          for_each = node.value.role_kind == "control-plane" ? [1] : []
          content {
            container_port = 443
            host_port      = 443
            protocol       = "TCP"
            listen_address = "127.0.0.1"
          }
        }

        # Control-plane only: kubeadm patch to label itself for ingress controllers,
        # which Traefik (Phase 2) will use as its node selector.
        kubeadm_config_patches = node.value.role_kind == "control-plane" ? [
          <<-EOT
            kind: InitConfiguration
            nodeRegistration:
              kubeletExtraArgs:
                node-labels: "ingress-ready=true"
          EOT
        ] : []
      }
    }
  }
}

###############################################################################
# Side-by-side kubeconfig with `server:` rewritten to localhost.
# The provider's kubeconfig points at 127.0.0.1:<random> by default, which works,
# but external `kubectl` sessions benefit from a stable host:port. kind writes
# the same content; we just rewrite it for ergonomics.
###############################################################################

data "local_file" "provider_kubeconfig" {
  filename   = abspath(var.kubeconfig_path)
  depends_on = [kind_cluster.cicd]
}

resource "local_file" "kubeconfig_localhost" {
  filename = abspath(var.kubeconfig_path)
  content = replace(
    replace(data.local_file.provider_kubeconfig.content, "server: https://127.0.0.1:", "server: https://localhost:"),
    "server: https://[::1]:", "server: https://localhost:"
  )
}

###############################################################################
# Smoke test: prove all nodes are Ready before declaring phase_ready=true.
# A null_resource with a `local-exec` provisioner does the work without dragging
# in a heavy `kubernetes` provider just for one command.
###############################################################################

resource "null_resource" "smoke_test" {
  depends_on = [kind_cluster.cicd, local_file.kubeconfig_localhost]

  triggers = {
    cluster = kind_cluster.cicd.id
  }

  provisioner "local-exec" {
    command = <<-EOT
      set -e
      KUBECONFIG='${abspath(var.kubeconfig_path)}' kubectl get nodes -o wide > '${path.module}/.last_smoke.json.tmp'
      mv '${path.module}/.last_smoke.json.tmp' '${path.module}/.last_smoke.json'
      KUBECONFIG='${abspath(var.kubeconfig_path)}' kubectl wait --for=condition=Ready node --all --timeout=180s
    EOT
  }
}