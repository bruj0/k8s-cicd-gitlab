output "kubeconfig_path" {
  description = "Path to the rewritten kubeconfig. Use KUBECONFIG=... kubectl ... to talk to the cluster."
  value       = abspath(var.kubeconfig_path)
}

output "cluster_name" {
  value = kind_cluster.cicd.name
}

output "node_names" {
  value       = [for n in local.nodes : n.name]
  description = "Names of every kind node, ordered as in node_shapes."
}

output "domain" {
  value = var.domain
}

output "ca_cert_path" {
  description = "Path to the local CA cert (Phase 2 will use this to sign cert-manager issuers)."
  value       = abspath("${path.module}/../tls/private/ca.crt")
}

output "ca_key_path" {
  description = "Path to the local CA private key. NEVER committed (gitignored)."
  value       = abspath("${path.module}/../tls/private/ca.key")
}

output "wildcard_cert_path" {
  value = abspath("${path.module}/../tls/private/_.${var.domain}.crt")
}

output "wildcard_key_path" {
  value       = abspath("${path.module}/../tls/private/_.${var.domain}.key")
}

output "phase_ready" {
  description = "True once the kind cluster is up and all nodes are Ready."
  value       = null_resource.smoke_test.id != ""
}