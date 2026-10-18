data "google_project" "p" {}

locals {
  # NOTE: secret VALUES are created out-of-band (see the "secret values not in TF"
  # convention below). fanvue-oauth-tokens holds the rotating OAuth refresh/access
  # token JSON, written by the one-time OAuth browser flow (scripts/fanvue_auth.py)
  # and then rotated by the running service. jenny-pg-password backs Cloud SQL.
  jenny_secrets = ["openrouter-api-key", "fanvue-client-secret", "fanvue-webhook-secret",
  "sim-secret", "fanvue-oauth-tokens", "jenny-pg-password"]

  apis = [
    "run.googleapis.com", "cloudbuild.googleapis.com", "artifactregistry.googleapis.com",
    "firestore.googleapis.com", "secretmanager.googleapis.com", "bigquery.googleapis.com",
    "cloudscheduler.googleapis.com", "iamcredentials.googleapis.com", "sts.googleapis.com",
    "eventarc.googleapis.com", "sqladmin.googleapis.com", "aiplatform.googleapis.com",
    "cloudtasks.googleapis.com", # per-fan debounce worker queue
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
    # Tier sized to hold the HNSW index in RAM and give the connection pool
    # headroom (micro's ~0.6GB RAM + tiny max_connections can't do either once
    # fan/vector volume grows). db-g1-small = 1.7GB. NOTE: changing tier RESTARTS
    # the instance (brief memory-layer downtime) — apply off-peak. Bump further
    # (db-custom-N-M) as the fact corpus grows.
    tier              = "db-g1-small"
    availability_type = "ZONAL"
    disk_size         = 10
    disk_autoresize   = true
    ip_configuration {
      ipv4_enabled = true # public IP; Cloud Run reaches it via the Cloud SQL socket
    }
    backup_configuration {
      enabled = true
    }
    # Cross-instance connection budget: each Cloud Run instance opens up to
    # MEMORY_PG_MAXCONN connections, and they SUM at Postgres. Keep
    # MEMORY_PG_MAXCONN x maxScale under this instance's max_connections, OR enable
    # Cloud SQL managed connection pooling (transaction mode) so the pooler
    # multiplexes many instances onto a few backend connections. Managed pooling is
    # toggled on the instance (console/gcloud) and is the right answer beyond a
    # handful of instances — see docs/debounce.md + memory layer notes.
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

# The runtime rotates the Fanvue OAuth token (single-use refresh tokens), so it
# must be able to ADD new versions of fanvue-oauth-tokens, not just read it.
resource "google_secret_manager_secret_iam_member" "runtime_fanvue_tokens_writer" {
  secret_id = "fanvue-oauth-tokens"
  role      = "roles/secretmanager.secretVersionAdder"
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

# Per-fan debounce (DEBOUNCE_ENABLED): the webhook records a fan message then
# enqueues a delayed POST to /admin/process_fan here. The queue waits out the
# settle window and retries (backoff) until the worker returns 2xx. maxDispatches
# bounds concurrency; retry window is generous so a transient LLM/Fanvue blip
# never drops a reply. See docs/debounce + agent/tasks.py.
resource "google_cloud_tasks_queue" "process" {
  name     = "jenny-process"
  location = var.region

  rate_limits {
    max_dispatches_per_second = 50
    max_concurrent_dispatches = 20
  }
  retry_config {
    max_attempts       = 8
    min_backoff        = "5s"
    max_backoff        = "120s"
    max_retry_duration = "1800s" # give up after 30m of retries
  }
  depends_on = [google_project_service.apis]
}

# Extraction fan-out (MEMORY_FANOUT_ENABLED): mem0 fact-extraction + history
# backfill run as their OWN tasks here (one per turn / per fan) instead of inline,
# so slow LLM-bound learning never blocks a reply or a reconcile run. Lower
# dispatch rate than the reply queue — extraction is background work and its rate
# is really bounded by the LLM provider, not us.
resource "google_cloud_tasks_queue" "extract" {
  name     = "jenny-extract"
  location = var.region

  rate_limits {
    max_dispatches_per_second = 10
    max_concurrent_dispatches = 10
  }
  retry_config {
    max_attempts       = 5
    min_backoff        = "10s"
    max_backoff        = "300s"
    max_retry_duration = "3600s"
  }
  depends_on = [google_project_service.apis]
}

# The runtime SA creates tasks on the queues (enqueue from the webhook / reconcile).
# The worker endpoints are authorized by the sim-secret header the task carries, so
# no run.invoker binding is needed while the service allows unauthenticated.
resource "google_project_iam_member" "runtime_tasks_enqueuer" {
  project = var.project
  role    = "roles/cloudtasks.enqueuer"
  member  = "serviceAccount:${google_service_account.runtime.email}"
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
