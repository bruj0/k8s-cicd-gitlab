terraform {
  required_version = ">= 1.6, < 2.0"

  required_providers {
    # Multinode kind cluster: https://search.opentofu.org/provider/tehcyx/kind
    kind = {
      source  = "tehcyx/kind"
      version = "~> 0.11"
    }

    # Pinned for Phase 2 smoke-test validation (Phase 2 itself drives `helm install`
    # via CommandRunner, not via this provider).
    helm = {
      source  = "hashicorp/helm"
      version = "~> 3.0"
    }

    # Used to emit a side-by-side kubeconfig with localhost endpoints so
    # external `kubectl` sessions can talk to the cluster without needing
    # the kind-provider port-forward.
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