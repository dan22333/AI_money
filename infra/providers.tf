terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
  # State lives in GCS (bucket created during bootstrap).
  backend "gcs" {
    bucket = "capsule-487202-tf-state"
    prefix = "jenny"
  }
}

provider "google" {
  project = var.project
  region  = var.region
}
