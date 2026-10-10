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

variable "service_url" {
  type        = string
  default     = "https://jenny-agent-x4zdyfvfia-uc.a.run.app"
  description = "Cloud Run service URL (stable for the service; the nightly reconcile posts here)"
}

variable "reconcile_sim_secret" {
  type        = string
  default     = ""
  sensitive   = true
  description = <<-EOT
    Value of sim-secret, injected as the x-sim-secret header on the nightly
    reconcile job. Provide at apply time (TF_VAR_reconcile_sim_secret=...), do
    NOT commit it. If left empty the scheduler job is not created.
  EOT
}
