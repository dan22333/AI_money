# Cloud Monitoring — alerts emailed to var.alert_email. Built-in metrics for
# infra health; log-based metrics for app-level failures (token refresh, send).
# Thresholds are deliberately conservative starting points — tune as real traffic
# gives you a baseline.

resource "google_project_service" "monitoring" {
  service            = "monitoring.googleapis.com"
  disable_on_destroy = false
}

resource "google_monitoring_notification_channel" "email" {
  display_name = "Jenny alerts (email)"
  type         = "email"
  labels       = { email_address = var.alert_email }
}

# ---- log-based metrics (count matching log lines the app prints) ----
resource "google_logging_metric" "token_refresh_failures" {
  name        = "jenny_token_refresh_failures"
  description = "Count of Fanvue OAuth token refresh failures"
  filter      = <<-EOT
    resource.type="cloud_run_revision"
    resource.labels.service_name="jenny-agent"
    (textPayload:"[refresh_all]" AND textPayload:"failed") OR textPayload:"delivery to Fanvue failed: HTTPStatusError"
  EOT
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
}

resource "google_logging_metric" "send_failures" {
  name        = "jenny_send_failures"
  description = "Count of Fanvue message send failures"
  filter      = <<-EOT
    resource.type="cloud_run_revision"
    resource.labels.service_name="jenny-agent"
    textPayload:"[send] delivery to Fanvue failed"
  EOT
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
}

# ---- alert policies ----
locals {
  alert_channels = [google_monitoring_notification_channel.email.id]
}

# The scale killer: Postgres backend connections approaching the instance limit.
resource "google_monitoring_alert_policy" "sql_connections" {
  display_name = "Cloud SQL connections high (jenny-pg)"
  combiner     = "OR"
  conditions {
    display_name = "num_backends > 80"
    condition_threshold {
      filter          = "resource.type=\"cloudsql_database\" AND resource.labels.database_id=\"${var.project}:jenny-pg\" AND metric.type=\"cloudsql.googleapis.com/database/postgresql/num_backends\""
      comparison      = "COMPARISON_GT"
      threshold_value = 80
      duration        = "300s"
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MEAN"
      }
    }
  }
  notification_channels = local.alert_channels
}

# Token refresh failing → Jenny can't send (bricked twice before).
resource "google_monitoring_alert_policy" "token_refresh" {
  display_name = "Fanvue token refresh failing"
  combiner     = "OR"
  conditions {
    display_name = "token refresh/send auth failures > 0"
    condition_threshold {
      filter          = "resource.type=\"cloud_run_revision\" AND metric.type=\"logging.googleapis.com/user/${google_logging_metric.token_refresh_failures.name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period   = "600s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }
  notification_channels = local.alert_channels
}

# Replies silently not delivered.
resource "google_monitoring_alert_policy" "send_failures" {
  display_name = "Fanvue message send failures"
  combiner     = "OR"
  conditions {
    display_name = "send failures > 0"
    condition_threshold {
      filter          = "resource.type=\"cloud_run_revision\" AND metric.type=\"logging.googleapis.com/user/${google_logging_metric.send_failures.name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period   = "600s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }
  notification_channels = local.alert_channels
}

# App broken: elevated 5xx from Cloud Run.
resource "google_monitoring_alert_policy" "http_5xx" {
  display_name = "jenny-agent 5xx errors"
  combiner     = "OR"
  conditions {
    display_name = "5xx responses > 5 in 5m"
    condition_threshold {
      filter          = "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"jenny-agent\" AND metric.type=\"run.googleapis.com/request_count\" AND metric.labels.response_code_class=\"5xx\""
      comparison      = "COMPARISON_GT"
      threshold_value = 5
      duration        = "0s"
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }
  notification_channels = local.alert_channels
}

# Reply backlog building in the debounce queue (tasks not draining).
resource "google_monitoring_alert_policy" "process_queue_depth" {
  display_name = "jenny-process queue backlog"
  combiner     = "OR"
  conditions {
    display_name = "queue depth > 200 for 10m"
    condition_threshold {
      filter          = "resource.type=\"cloud_tasks_queue\" AND resource.labels.queue_id=\"jenny-process\" AND metric.type=\"cloudtasks.googleapis.com/queue/depth\""
      comparison      = "COMPARISON_GT"
      threshold_value = 200
      duration        = "600s"
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MEAN"
      }
    }
  }
  notification_channels = local.alert_channels
}

# Latency regression (p95 request latency).
resource "google_monitoring_alert_policy" "latency_p95" {
  display_name = "jenny-agent p95 latency high"
  combiner     = "OR"
  conditions {
    display_name = "p95 request latency > 15s for 5m"
    condition_threshold {
      filter          = "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"jenny-agent\" AND metric.type=\"run.googleapis.com/request_latencies\""
      comparison      = "COMPARISON_GT"
      threshold_value = 15000
      duration        = "300s"
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_PERCENTILE_95"
      }
    }
  }
  notification_channels = local.alert_channels
}
