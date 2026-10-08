#!/usr/bin/env bash
# IAM + Workload Identity Federation for keyless GitHub Actions -> Cloud Run deploys.
# Review the roles below before running. Safe to re-run.
set -uo pipefail

PROJECT="capsule-487202"
GH_REPO="dan22333/AI_money"
POOL="github-pool"
PROVIDER="github-provider"
DEPLOY_SA="jenny-deployer"

PROJNUM="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
SA_EMAIL="${DEPLOY_SA}@${PROJECT}.iam.gserviceaccount.com"
RUNTIME_SA="${PROJNUM}-compute@developer.gserviceaccount.com"
echo "project=$PROJECT number=$PROJNUM"

echo "### deploy service account"
gcloud iam service-accounts create "$DEPLOY_SA" --project="$PROJECT" \
  --display-name="Jenny GitHub deployer" 2>&1 | tail -1 || echo "  (exists)"

echo "### roles for deploy SA (build + push + deploy)"
for role in roles/run.admin roles/cloudbuild.builds.editor roles/artifactregistry.writer \
            roles/iam.serviceAccountUser roles/storage.admin roles/serviceusage.serviceUsageConsumer; do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:${SA_EMAIL}" --role="$role" --condition=None >/dev/null 2>&1 \
    && echo "  + $role" || echo "  ! failed $role"
done

echo "### runtime SA (Cloud Run) can read secrets + Firestore"
for role in roles/secretmanager.secretAccessor roles/datastore.user; do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:${RUNTIME_SA}" --role="$role" --condition=None >/dev/null 2>&1 \
    && echo "  + $role (runtime)" || echo "  ! failed $role"
done

echo "### Workload Identity Federation"
gcloud iam workload-identity-pools create "$POOL" --project="$PROJECT" \
  --location=global --display-name="GitHub pool" 2>&1 | tail -1 || echo "  (pool exists)"
gcloud iam workload-identity-pools providers create-oidc "$PROVIDER" \
  --project="$PROJECT" --location=global --workload-identity-pool="$POOL" \
  --display-name="GitHub provider" \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
  --attribute-condition="assertion.repository=='${GH_REPO}'" 2>&1 | tail -1 || echo "  (provider exists)"
gcloud iam service-accounts add-iam-policy-binding "$SA_EMAIL" --project="$PROJECT" \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/${PROJNUM}/locations/global/workloadIdentityPools/${POOL}/attribute.repository/${GH_REPO}" \
  >/dev/null 2>&1 && echo "  bound repo -> SA" || echo "  ! binding failed"

echo
echo "================= set these as GitHub repo secrets ================="
echo "GCP_PROJECT      = ${PROJECT}"
echo "GCP_REGION       = us-central1"
echo "GCP_DEPLOY_SA    = ${SA_EMAIL}"
echo "GCP_WIF_PROVIDER = projects/${PROJNUM}/locations/global/workloadIdentityPools/${POOL}/providers/${PROVIDER}"
echo "==================================================================="
