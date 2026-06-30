terraform {
  required_version = ">= 1.6, < 2.0"

  required_providers {
    # Multinode kind cluster: https://search.opentofu.org/provider/tehcyx/kind
    kind = {
      source  = "tehcyx/kind"
      version = "~> 0.11"
    }

    # Pinned for Phase 2 (GitLab + OpenBao + Traefik will be installed via helm_release).
    helm = {
      source  = "hashicorp/helm"
      version = "~> 3.0"
    }

    # Used to (a) emit a side-by-side kubeconfig with localhost endpoints,
    # (b) stage the wildcard cert into a path cert-manager can pick up later.
    local = {
      source  = "hashicorp/local"
      version = "~> 2.5"
    }

    # Used to run the post-apply smoke test.
    null = {
      source  = "hashicorp/null"
      version = "~> 3.2"
    }
  }
}

# Defaults are fine; declaring the providers makes the file the source of truth.
provider "kind" {}
provider "helm" {}
provider "local" {}