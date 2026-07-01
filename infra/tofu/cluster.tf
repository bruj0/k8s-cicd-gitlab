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

        # Shared hostPath bind for the `local-path` StorageClass
        # (pathBase = /var/local/shared, host source = infra/data/shared).
        # Each service (postgres, gitaly, registry, minio, redis, kas,
        # prometheus, rails/uploads, openbao) gets its own sub-directory
        # under infra/data/shared.
        #
        # Bind propagation toggle — bidirectional mode contract:
        #   - var.preserve_stateful_data = false (default, 2026-07+):
        #     propagation = "Bidirectional". `tofu destroy` tears the
        #     whole cluster down AND wipes the host-side data — chart-
        #     managed PVC dirs go via local-path's default `rm -rf`
        #     teardown, hostPath-backed dirs (infra/data/shared/stable/*)
        #     go via the null_resource.wipe_data destroy provisioner
        #     below. The "kernel sweeps the source on container umount"
        #     (which historically warned against Bidirectional) is
        #     actually the desired behaviour here.
        #   - var.preserve_stateful_data = true (legacy, 2026-06 and
        #     earlier): propagation omitted (default None /
        #     HostToContainer). Cluster recreate leaves infra/data/shared/*
        #     intact. Users still need to manually re-bind the dirs +
        #     re-apply the chart-managed Secrets snapshot, AND the
        #     local-path teardown script must be re-patched to `mv` —
        #     see infra/scripts/bootstrap/phase2/local_path_provisioner.py
        #     module docstring for the full list of re-coupling points.
        #
        # The earlier "IMPORTANT: do NOT set propagation=..." warning
        # was correct for the old contract (preserve across recreate).
        # It is preserved below as a deliberate history note so a future
        # reader who flips the contract back understands WHY the
        # toggle exists and what the kernel-vs-explicit-provisioner
        # trade-off is.
        extra_mounts {
          host_path      = local.shared_host_abs
          container_path = "/var/local/shared"
          read_only      = false
          propagation    = var.preserve_stateful_data ? null : "Bidirectional"
        }

        labels = {
          for l in node.value.labels : split("=", l)[0] => split("=", l)[1]
        }

        # Only the control-plane reserves host ports 80/443/22 so the
        # chart-managed Envoy Gateway can serve the *.local.<domain>
        # wildcard on the same address that /etc/hosts binds it to. Putting
        # the mapping on every node would make docker reject the second
        # container that tries to claim 127.0.0.1:80.
        #
        # Why containerPort is in the 30000 range (kind's NodePort band):
        # chart 10.x's bundled envoy-gateway sub-chart defaults the
        # gateway-api Service to ClusterIP (no external address), which
        # leaves Gateway.spec.addresses=127.0.0.1 in `AddressNotUsable`
        # state. We override the Service to NodePort via
        # helm-values-gitlab.yaml (global.gatewayApi.service.type=NodePort
        # + matching `nodePorts: {http: 30080, https: 30443, ssh: 30022}`),
        # and pair that with these host-port mappings so the host's
        # port 80 → control-plane NodePort 30080 → envoy data-plane.
        dynamic "extra_port_mappings" {
          for_each = node.value.role_kind == "control-plane" ? [1] : []
          content {
            container_port = 30080
            host_port      = 80
            protocol       = "TCP"
            listen_address = "127.0.0.1"
          }
        }
        dynamic "extra_port_mappings" {
          for_each = node.value.role_kind == "control-plane" ? [1] : []
          content {
            container_port = 30443
            host_port      = 443
            protocol       = "TCP"
            listen_address = "127.0.0.1"
          }
        }
        # gitlab-shell SSH listener (also goes through the chart's
        # Envoy Gateway + ssh-listener, hence the NodePort mapping).
        dynamic "extra_port_mappings" {
          for_each = node.value.role_kind == "control-plane" ? [1] : []
          content {
            container_port = 30022
            host_port      = 22
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

###############################################################################
# Bidirectional-mode destroy hook.
#
# `tofu destroy` normally only tears down resources declared in this
# module (the kind cluster + the side-by-side kubeconfig). The host-side
# data under infra/data/shared/ — the hostPath PV backing dir, plus
# chart-managed PVC dirs that local-path-provisioner creates via the
# bind-mount — is invisible to terraform. Without this hook, `tofu
# destroy && apply` would leave those dirs behind and the next cluster
# would re-bind against stale state.
#
# We pair with the local-path default teardown script (`rm -rf`) so:
#   1. `tofu destroy` → cluster is removed → local-path's teardown
#      `rm -rf`s each PVC's host dir.
#   2. `tofu destroy` → null_resource.destroy hook below → sweeps the
#      infra/data/shared/stable/* hostPath dirs the bootstrap
#      pre-creates (CloudNativePG, Redis, MinIO, OpenBao, Gitaly) that
#      local-path-provisioner doesn't know about.
#
# Skipped when var.preserve_stateful_data = true (the legacy contract,
# for users who want `tofu destroy` to leave data behind).
###############################################################################

resource "null_resource" "wipe_data" {
  depends_on = [kind_cluster.cicd]

  triggers = {
    cluster = kind_cluster.cicd.id
    preserve_stateful_data = tostring(var.preserve_stateful_data)
    data_root = abspath(var.data_root)
  }

  # Nothing to do on apply — only on destroy.
  #
  # Destroy-time provisioners can only reference `self.*`, `count.index`,
  # and `each.key` (not `var.*` or `path.*`). We thread the toggle
  # through `triggers` (which IS `self`) so the shell script can
  # read it. If `var.preserve_stateful_data` flips between apply and
  # destroy, the trigger value changes, tofu destroys + recreates this
  # null_resource, and the new value runs on the next destroy — which
  # is the desired "flag was true at apply-time AND destroy-time, so
  # preserve" semantics.
  provisioner "local-exec" {
    when = destroy
    command = <<-EOT
      set -e
      if [ "${self.triggers.preserve_stateful_data}" = "true" ]; then
        echo "[wipe_data] preserve_stateful_data=true — leaving infra/data/shared/ intact"
        exit 0
      fi
      SHARED='${abspath(self.triggers.data_root)}/shared'
      if [ -d "$SHARED" ]; then
        echo "[wipe_data] removing $SHARED"
        # Pod-UID-owned leftovers may resist `rm -rf`; fall back
        # to a privileged container with the host dir bind-
        # mounted (same trick the bootstrap's --destroy uses —
        # see infra/scripts/bootstrap/cli.py).
        if ! rm -rf "$SHARED"; then
          echo "[wipe_data] unprivileged rm failed, falling back to privileged container"
          docker run --rm \
            -v "${abspath(self.triggers.data_root)}:/data" \
            alpine sh -c "rm -rf /data/shared/*" || true
        fi
      fi
    EOT
  }
}