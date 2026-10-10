data "google_project" "p" {}

locals {
  jenny_secrets = ["openrouter-api-key", "fanvue-client-secret", "fanvue-webhook-secret", "sim-secret"]

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

# Isolated DB for synthetic `sim:` traffic (/simulate + the canary smoke test),
# so test data never touches the production (default) database. Same project,
# so the runtime SA's project-level datastore.user already covers it.
resource "google_firestore_database" "staging" {
  name        = "staging"
  location_id = var.region
  type        = "FIRESTORE_NATIVE"
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

# Dedicated least-privilege runtime identity for the Cloud Run service.
resource "google_service_account" "runtime" {
  account_id   = "jenny-runtime"
  display_name = "Jenny Cloud Run runtime"
}

# Firestore access (single project DB).
resource "google_project_iam_member" "runtime_datastore" {
  project = var.project
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.runtime.email}"
}

# Secret access scoped to ONLY Jenny's secrets (resource-level, not project-wide).
resource "google_secret_manager_secret_iam_member" "runtime_secrets" {
  for_each  = toset(local.jenny_secrets)
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime.email}"
}

# The deploy pipeline reads sim-secret to run the real-model smoke test against
# the canary revision before shifting traffic.
resource "google_secret_manager_secret_iam_member" "deployer_sim_secret" {
  secret_id = "sim-secret"
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.deployer.email}"
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

# Nightly reconcile: Cloud Scheduler POSTs /admin/reconcile so missed webhooks
# (subscribers, payments) get repaired from the Fanvue API once a day. Created
# only when reconcile_sim_secret is supplied at apply time; the secret value is
# never committed (consistent with the no-secret-values-in-TF policy, though it
# does transit TF state — provide via TF_VAR_reconcile_sim_secret).
resource "google_cloud_scheduler_job" "nightly_reconcile" {
  count     = var.reconcile_sim_secret == "" ? 0 : 1
  name      = "jenny-nightly-reconcile"
  region    = var.region
  schedule  = "0 4 * * *" # 04:00 daily
  time_zone = "Etc/UTC"

  http_target {
    http_method = "POST"
    uri         = "${var.service_url}/admin/reconcile"
    headers = {
      "x-sim-secret" = var.reconcile_sim_secret
      "Content-Type" = "application/json"
    }
    body = base64encode("{}")
  }

  retry_config {
    retry_count = 2
  }
}

# NOTE: Secret Manager secret VALUES are intentionally NOT managed by Terraform.
# The secret resources were created via gcloud and their versions are managed by
# gcloud/CI, so sensitive values never live in TF state.
