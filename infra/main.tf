data "google_project" "p" {}

locals {
  runtime_sa = "${data.google_project.p.number}-compute@developer.gserviceaccount.com"

  apis = [
    "run.googleapis.com", "cloudbuild.googleapis.com", "artifactregistry.googleapis.com",
    "firestore.googleapis.com", "secretmanager.googleapis.com", "bigquery.googleapis.com",
    "cloudscheduler.googleapis.com", "iamcredentials.googleapis.com", "sts.googleapis.com",
    "eventarc.googleapis.com",
  ]

  deployer_roles = [
    "roles/run.admin", "roles/cloudbuild.builds.editor", "roles/artifactregistry.writer",
    "roles/iam.serviceAccountUser", "roles/storage.admin", "roles/serviceusage.serviceUsageConsumer",
  ]

  runtime_roles = ["roles/secretmanager.secretAccessor", "roles/datastore.user"]
}

# Enable APIs (safe on a shared project: never disabled on destroy).
resource "google_project_service" "apis" {
  for_each           = toset(local.apis)
  service            = each.value
  disable_on_destroy = false
}

# ---- resources that already exist: import once (see import.sh) ----
resource "google_artifact_registry_repository" "jenny" {
  location      = var.region
  repository_id = var.repo_name
  format        = "DOCKER"
}

resource "google_firestore_database" "default" {
  name        = "(default)"
  location_id = var.region
  type        = "FIRESTORE_NATIVE"
  lifecycle {
    prevent_destroy = true
  }
}

# ---- new resources TF creates ----
resource "google_service_account" "deployer" {
  account_id   = "jenny-deployer"
  display_name = "Jenny GitHub deployer"
}

# Additive (_member, not _policy) so we never clobber other apps' IAM in this shared project.
resource "google_project_iam_member" "deployer" {
  for_each = toset(local.deployer_roles)
  project  = var.project
  role     = each.value
  member   = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_project_iam_member" "runtime" {
  for_each = toset(local.runtime_roles)
  project  = var.project
  role     = each.value
  member   = "serviceAccount:${local.runtime_sa}"
}

resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "github-pool"
  display_name              = "GitHub pool"
}

resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github-provider"
  display_name                       = "GitHub provider"
  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
  }
  attribute_condition = "assertion.repository == '${var.github_repo}'"
  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account_iam_member" "wif_bind" {
  service_account_id = google_service_account.deployer.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/projects/${data.google_project.p.number}/locations/global/workloadIdentityPools/${google_iam_workload_identity_pool.github.workload_identity_pool_id}/attribute.repository/${var.github_repo}"
}

# Analytics warehouse (Firestore exports land here in Phase 2).
resource "google_bigquery_dataset" "analytics" {
  dataset_id = "jenny_analytics"
  location   = "US"
}

# NOTE: Secret Manager secret VALUES are intentionally NOT managed by Terraform.
# The secret resources were created via gcloud and their versions are managed by
# gcloud/CI, so sensitive values never live in TF state.
