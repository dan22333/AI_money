#!/usr/bin/env bash
# One-time GCP provisioning for the Jenny agent.
# Idempotent-ish: re-running is safe (creates are guarded).
# Reads secret VALUES from secrets/.env (never prints them).
set -uo pipefail

PROJECT="capsule-487202"
REGION="us-central1"
REPO="jenny"                      # Artifact Registry repo
SERVICE="jenny-agent"             # Cloud Run service
GH_REPO="dan22333/AI_money"       # GitHub repo for WIF binding
POOL="github-pool"
PROVIDER="github-provider"
DEPLOY_SA="jenny-deployer"

cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
set -a; . secrets/.env; set +a

PROJNUM="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
echo "project=$PROJECT number=$PROJNUM region=$REGION"

echo "### Firestore (native)"
gcloud firestore databases create --location="$REGION" --project="$PROJECT" 2>&1 | tail -1 \
  || echo "  (firestore db may already exist)"

echo "### Artifact Registry"
gcloud artifacts repositories create "$REPO" --repository-format=docker \
  --location="$REGION" --project="$PROJECT" 2>&1 | tail -1 || echo "  (repo exists)"

echo "### Secret Manager"
create_secret () {  # name value
  local name="$1" val="$2"
  if gcloud secrets describe "$name" --project="$PROJECT" >/dev/null 2>&1; then
    printf "%s" "$val" | gcloud secrets versions add "$name" --data-file=- --project="$PROJECT" >/dev/null
    echo "  updated $name"
  else
    printf "%s" "$val" | gcloud secrets create "$name" --data-file=- --project="$PROJECT" >/dev/null
    echo "  created $name"
  fi
}
create_secret openrouter-api-key   "${OPENROUTER_API_KEY:-}"
create_secret fanvue-client-secret "${OAUTH_CLIENT_SECRET:-}"
create_secret sim-secret           "$(openssl rand -hex 24)"
# placeholder until Fanvue gives us the real webhook signing secret
gcloud secrets describe fanvue-webhook-secret --project="$PROJECT" >/dev/null 2>&1 \
  || create_secret fanvue-webhook-secret "placeholder-set-after-webhook-registration"

echo "### Deploy service account"
SA_EMAIL="${DEPLOY_SA}@${PROJECT}.iam.gserviceaccount.com"
gcloud iam service-accounts create "$DEPLOY_SA" --project="$PROJECT" \
  --display-name="Jenny GitHub deployer" 2>&1 | tail -1 || echo "  (SA exists)"

for role in roles/run.admin roles/cloudbuild.builds.editor roles/artifactregistry.writer \
            roles/iam.serviceAccountUser roles/storage.admin roles/serviceusage.serviceUsageConsumer; do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:${SA_EMAIL}" --role="$role" --condition=None >/dev/null 2>&1 \
    && echo "  granted $role" || echo "  (failed $role)"
done

echo "### Runtime service account perms (Cloud Run uses default compute SA)"
RUNTIME_SA="${PROJNUM}-compute@developer.gserviceaccount.com"
for role in roles/secretmanager.secretAccessor roles/datastore.user; do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:${RUNTIME_SA}" --role="$role" --condition=None >/dev/null 2>&1 \
    && echo "  granted $role to runtime SA" || echo "  (failed $role)"
done

echo "### Workload Identity Federation (keyless GitHub -> GCP)"
gcloud iam workload-identity-pools create "$POOL" --project="$PROJECT" \
  --location=global --display-name="GitHub pool" 2>&1 | tail -1 || echo "  (pool exists)"

gcloud iam workload-identity-pools providers create-oidc "$PROVIDER" \
  --project="$PROJECT" --location=global --workload-identity-pool="$POOL" \
  --display-name="GitHub provider" \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
  --attribute-condition="assertion.repository=='${GH_REPO}'" 2>&1 | tail -1 || echo "  (provider exists)"

# allow this repo to impersonate the deploy SA
gcloud iam service-accounts add-iam-policy-binding "$SA_EMAIL" --project="$PROJECT" \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/${PROJNUM}/locations/global/workloadIdentityPools/${POOL}/attribute.repository/${GH_REPO}" \
  >/dev/null 2>&1 && echo "  bound repo -> SA" || echo "  (binding failed)"

WIF_PROVIDER="projects/${PROJNUM}/locations/global/workloadIdentityPools/${POOL}/providers/${PROVIDER}"

echo
echo "================= GITHUB REPO SECRETS (set these) ================="
echo "GCP_PROJECT     = ${PROJECT}"
echo "GCP_REGION      = ${REGION}"
echo "GCP_DEPLOY_SA   = ${SA_EMAIL}"
echo "GCP_WIF_PROVIDER= ${WIF_PROVIDER}"
echo "=================================================================="
