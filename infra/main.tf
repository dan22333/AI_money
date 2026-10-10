data "google_project" "p" {}

locals {
  jenny_secrets = ["openrouter-api-key", "fanvue-client-secret", "fanvue-webhook-secret",
  "sim-secret", "jenny-pg-password"]

  apis = [
    "run.googleapis.com", "cloudbuild.googleapis.com", "artifactregistry.googleapis.com",
    "firestore.googleapis.com", "secretmanager.googleapis.com", "bigquery.googleapis.com",
    "cloudscheduler.googleapis.com", "iamcredentials.googleapis.com", "sts.googleapis.com",
    "eventarc.googleapis.com", "sqladmin.googleapis.com", "aiplatform.googleapis.com",
  ]

  deployer_roles = [
    "roles/run.admin", "roles/cloudbuild.builds.editor", "roles/artifactregistry.writer",
    "roles/iam.serviceAccountUser", "roles/storage.admin", "roles/serviceusage.serviceUsageConsumer",
    "roles/cloudsql.client", # deploy's migrate job connects via the Cloud SQL Auth Proxy
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

# Long-term memory needs: connect to Cloud SQL (pgvector) + call Vertex AI
# embeddings. Both additive (_member) so we never clobber shared-project IAM.
resource "google_project_iam_member" "runtime_cloudsql" {
  project = var.project
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_project_iam_member" "runtime_vertex" {
  project = var.project
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.runtime.email}"
}

# Cloud SQL Postgres for the self-hosted mem0 vector store (pgvector).
# Smallest tier; the memory workload is tiny (a few vectors per fan).
resource "google_sql_database_instance" "jenny_pg" {
  name             = "jenny-pg"
  database_version = "POSTGRES_15"
  region           = var.region
  settings {
    tier              = "db-f1-micro"
    availability_type = "ZONAL"
    disk_size         = 10
    disk_autoresize   = true
    ip_configuration {
      ipv4_enabled = true # public IP; Cloud Run reaches it via the Cloud SQL socket
    }
    backup_configuration {
      enabled = true
    }
  }
  # Prevent accidental teardown of the fan-memory database.
  deletion_protection = true
}

resource "google_sql_database" "jenny" {
  name     = "jenny"
  instance = google_sql_database_instance.jenny_pg.name
}

# NOTE: the `postgres` user's password is set out-of-band (gcloud) and stored in
# the jenny-pg-password secret — kept out of TF state, like all other secrets.
# One-time, after apply:
#   gcloud sql users set-password postgres --instance=jenny-pg --password="$PW"
#   printf '%s' "$PW" | gcloud secrets create jenny-pg-password --data-file=-
# Then, once (enables pgvector; mem0 creates its own table):
#   gcloud sql connect jenny-pg --user=postgres --database=jenny \
#     -c 'CREATE EXTENSION IF NOT EXISTS vector;'

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

# The deploy migrate job reads the Postgres password to run alembic.
resource "google_secret_manager_secret_iam_member" "deployer_pg_password" {
  secret_id = "jenny-pg-password"
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

# NOTE: Secret Manager secret VALUES are intentionally NOT managed by Terraform.
# The secret resources were created via gcloud and their versions are managed by
# gcloud/CI, so sensitive values never live in TF state.
