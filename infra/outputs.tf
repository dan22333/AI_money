output "wif_provider" {
  description = "Set as GitHub repo secret GCP_WIF_PROVIDER"
  value       = "projects/${data.google_project.p.number}/locations/global/workloadIdentityPools/${google_iam_workload_identity_pool.github.workload_identity_pool_id}/providers/${google_iam_workload_identity_pool_provider.github.workload_identity_pool_provider_id}"
}

output "deployer_sa" {
  description = "Set as GitHub repo secret GCP_DEPLOY_SA"
  value       = google_service_account.deployer.email
}

output "runtime_sa" {
  description = "Cloud Run runs as this (set via --service-account in deploy)"
  value       = google_service_account.runtime.email
}
