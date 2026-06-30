# Host prerequisites

Phase 1 runs on any Linux or macOS host that can run Docker and `kind`. The
`bootstrap.py` helper will detect what's missing and install it for you.

## Supported operating systems

| OS              | Package manager | Notes                                                  |
| --------------- | --------------- | ------------------------------------------------------ |
| Ubuntu 22.04/24.04 | `apt`         | Tested                                                 |
| Debian 12       | `apt`           | Tested                                                 |
| Fedora 41       | `dnf`           | Tested                                                 |
| Arch / Manjaro  | `pacman`        | Tested on this host (`opentofu` is in `[extra]`)       |
| macOS 14/15     | `brew`          | Docker Desktop required for the daemon                 |

For any other distro, install the binaries manually — the rest of the
pipeline doesn't care how you got them.

## Hardware floor

- **RAM**: 24 GB free. The kind spec reserves 4 + 4 + 4 + 8 + 4 = 24 GB.
  Lower `node_shapes.memory` in `tofu.tfvars` if you have less.
- **CPU**: 4 cores minimum. The spec targets 2+2+2+4+2 = 12 cores; kind
  shares the host's cores, so lower numbers are fine.
- **Disk**: 30 GB free in the workspace. `data/*` mount points persist
  on the host; the kind cluster itself lives inside Docker.
- **Docker daemon**: must be reachable (`docker info` succeeds). On Linux,
  `sudo systemctl start docker`. On macOS, launch Docker Desktop.

## Ports

| Port | Purpose                                                  |
| ---- | -------------------------------------------------------- |
| 80   | Reserved for Phase 2 Traefik HTTP entrypoint            |
| 443  | Reserved for Phase 2 Traefik HTTPS entrypoint           |
| 6443 | kind control-plane API (mapped to a random host port)   |

## Toolchain (any of these installable via `bootstrap.py`)

- `docker` — container runtime for kind nodes
- `kubectl` — cluster access
- `kind` ≥ 0.27 — node image: `kindest/node:v1.31.0`
- `helm` ≥ 3 — pinned for Phase 2 (not used in Phase 1)
- `tofu` ≥ 1.6 (OpenTofu) — IaC runtime
- `openssl` ≥ 3 — PKI generation in `pki.py`

## DNS

Phase 1 does **not** require public DNS. `*.local.bruj0.net` is intended to
resolve to `127.0.0.1` once you add a hosts entry on your laptop:

```
127.0.0.1  gitlab.local.bruj0.net traefik.local.bruj0.net openbao.local.bruj0.net
```

Phase 2 will install Traefik and Gateway API. Until then, the kubeconfig is
all you need.