# Infrastructure (Terraform)

Declarative GCP infra for the Jenny agent. Supersedes the imperative
`scripts/gcp_setup.sh` / `scripts/gcp_iam.sh` (kept only as reference).

## What it manages
- Enabled APIs (never disabled on destroy — safe on this shared project)
- Artifact Registry repo `jenny`
- Firestore (native) — imported, `prevent_destroy`
- `jenny-deployer` service account + its deploy roles
- Runtime SA (Cloud Run) roles: Secret accessor + Firestore user
- Workload Identity Federation (GitHub → GCP, keyless)
- BigQuery dataset `jenny_analytics`

## What it deliberately does NOT manage
- **Secret values** — the Secret Manager secrets' values are managed via gcloud/CI,
  so nothing sensitive lands in TF state.
- **The Cloud Run service / image** — revisions are deployed by CI
  (`gcloud run deploy`), so TF and the deploy pipeline never fight over the image.

## First run

```bash
# one-time: app-default creds for the Google provider
gcloud auth application-default login

cd infra
./import.sh          # init + import the already-created AR repo + Firestore
terraform plan       # review — should be only NEW resources (SA, IAM, WIF, BQ)
terraform apply      # you run this — it makes IAM changes (your authority)
```

`terraform apply` prints `wif_provider` and `deployer_sa` — already set as GitHub
repo secrets, so nothing to copy unless they change.

## State
GCS backend: `gs://capsule-487202-tf-state` (prefix `jenny`).

## IAM note
`apply` performs IAM + WIF changes, so it must be run by someone with project
IAM authority (same reason the agent can't do it automatically).
