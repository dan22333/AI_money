#!/usr/bin/env bash
# Import resources that already exist (created during bootstrap) so the first
# `terraform plan` shows no changes for them. Run once, from infra/.
set -euo pipefail
cd "$(dirname "$0")"

terraform init
terraform import google_artifact_registry_repository.jenny \
  "projects/capsule-487202/locations/us-central1/repositories/jenny" || true
terraform import 'google_firestore_database.default' \
  "projects/capsule-487202/databases/(default)" || true

echo "Imported. Now: terraform plan   (then terraform apply)"
