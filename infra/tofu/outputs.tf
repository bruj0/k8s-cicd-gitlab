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
  value       = var.domain
  description = "*.wildcard the Phase 2 GitLab installer uses for global.hosts.domain (e.g. local.bruj0.net)."
}

output "data_root" {
  value       = abspath(var.data_root)
  description = "Where the Phase 2 bootstrap places PersistentVolume data. Must be writable by the user running `tofu apply`."
}

output "phase_ready" {
  description = "True once the kind cluster is up and all nodes are Ready."
  value       = null_resource.smoke_test.id != ""
}