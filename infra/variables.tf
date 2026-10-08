variable "project" {
  type    = string
  default = "capsule-487202"
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "github_repo" {
  type        = string
  default     = "dan22333/AI_money"
  description = "owner/repo allowed to deploy via Workload Identity Federation"
}

variable "repo_name" {
  type        = string
  default     = "jenny"
  description = "Artifact Registry repository id"
}
