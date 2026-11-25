# Observability, all GCP-native and free-tier: traces in Cloud Trace (see services/agent/cypress/tracing.py),
# logs in Cloud Logging, errors in Error Reporting, and this Cloud Monitoring dashboard built from
# log-based metrics on the structured lines the API already writes ("llm call", "chat turn").
# Cost figures here are visible only to the project's GCP principals, never to board or staff users.

locals {
  llm_filter  = "jsonPayload.message=~\"^llm call \" AND jsonPayload.\"gen_ai.operation.name\"=~\".\""
  turn_filter = "jsonPayload.\"cypress.event\"=\"chat_turn\""
}

resource "google_logging_metric" "llm_calls" {
  name   = "cypress/llm_calls"
  filter = local.llm_filter
  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "INT64"
    display_name = "Cypress model calls"
    labels {
      key = "step"
    }
    labels {
      key = "model"
    }
    labels {
      key = "ok"
    }
  }
  label_extractors = {
    step  = "EXTRACT(jsonPayload.\"gen_ai.operation.name\")"
    model = "EXTRACT(jsonPayload.\"gen_ai.request.model\")"
    ok    = "EXTRACT(jsonPayload.\"cypress.ok\")"
  }
}

resource "google_logging_metric" "llm_cost" {
  name            = "cypress/llm_cost_usd"
  filter          = "${local.llm_filter} AND jsonPayload.\"cypress.cost_usd\">0"
  value_extractor = "EXTRACT(jsonPayload.\"cypress.cost_usd\")"
  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "DISTRIBUTION"
    unit         = "USD"
    display_name = "Cypress model spend"
    labels {
      key = "step"
    }
  }
  label_extractors = {
    step = "EXTRACT(jsonPayload.\"gen_ai.operation.name\")"
  }
  bucket_options {
    exponential_buckets {
      num_finite_buckets = 20
      growth_factor      = 2
      scale              = 0.00001
    }
  }
}

resource "google_logging_metric" "turn_latency" {
  name            = "cypress/chat_turn_latency"
  filter          = local.turn_filter
  value_extractor = "EXTRACT(jsonPayload.latency_ms)"
  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "DISTRIBUTION"
    unit         = "ms"
    display_name = "Cypress chat turn latency"
    labels {
      key = "intent"
    }
    labels {
      key = "channel"
    }
    labels {
      key = "error"
    }
  }
  label_extractors = {
    intent  = "EXTRACT(jsonPayload.\"cypress.intent\")"
    channel = "EXTRACT(jsonPayload.\"cypress.channel\")"
    error   = "EXTRACT(jsonPayload.\"cypress.error\")"
  }
  bucket_options {
    exponential_buckets {
      num_finite_buckets = 16
      growth_factor      = 1.6
      scale              = 250
    }
  }
}

locals {
  m_turns = "logging.googleapis.com/user/${google_logging_metric.turn_latency.name}"
  m_calls = "logging.googleapis.com/user/${google_logging_metric.llm_calls.name}"
  m_cost  = "logging.googleapis.com/user/${google_logging_metric.llm_cost.name}"

  xy = { for k, v in {
    turns = {
      title = "Chat turns by intent (per hour)"
      ts = { filter = "metric.type=\"${local.m_turns}\"", aggregation = {
        alignmentPeriod = "3600s", perSeriesAligner = "ALIGN_COUNT", crossSeriesReducer = "REDUCE_SUM",
      groupByFields = ["metric.label.intent"] } }
      plot = "STACKED_BAR"
    }
    latency = {
      title = "Chat turn latency, p50 and p95 (ms)"
      multi = [
        { filter = "metric.type=\"${local.m_turns}\"", aggregation = {
        alignmentPeriod = "3600s", perSeriesAligner = "ALIGN_DELTA", crossSeriesReducer = "REDUCE_PERCENTILE_50" } },
        { filter = "metric.type=\"${local.m_turns}\"", aggregation = {
        alignmentPeriod = "3600s", perSeriesAligner = "ALIGN_DELTA", crossSeriesReducer = "REDUCE_PERCENTILE_95" } },
      ]
      plot = "LINE"
    }
    calls = {
      title = "Model calls by step (per hour)"
      ts = { filter = "metric.type=\"${local.m_calls}\"", aggregation = {
        alignmentPeriod = "3600s", perSeriesAligner = "ALIGN_SUM", crossSeriesReducer = "REDUCE_SUM",
      groupByFields = ["metric.label.step"] } }
      plot = "STACKED_BAR"
    }
    failed = {
      title = "Failed model calls (fallbacks covered them)"
      ts = { filter = "metric.type=\"${local.m_calls}\" AND metric.label.ok=\"false\"", aggregation = {
        alignmentPeriod = "3600s", perSeriesAligner = "ALIGN_SUM", crossSeriesReducer = "REDUCE_SUM",
      groupByFields = ["metric.label.model"] } }
      plot = "STACKED_BAR"
    }
    api = {
      title = "API requests by response class"
      ts = { filter = "metric.type=\"run.googleapis.com/request_count\" AND resource.type=\"cloud_run_revision\"", aggregation = {
        alignmentPeriod = "3600s", perSeriesAligner = "ALIGN_SUM", crossSeriesReducer = "REDUCE_SUM",
      groupByFields = ["metric.label.response_code_class", "resource.label.service_name"] } }
      plot = "STACKED_BAR"
    }
  } : k => v }
}

resource "google_monitoring_dashboard" "cypress" {
  dashboard_json = jsonencode({
    displayName = "Cypress"
    mosaicLayout = {
      columns = 48
      # The API drops zero coordinates; leaving them out keeps plans clean.
      tiles = [for t in concat(
        [for i, k in ["turns", "latency", "calls", "failed", "api"] : {
          xPos = (i % 2) * 24, yPos = floor(i / 2) * 16, width = 24, height = 16
          widget = {
            title = local.xy[k].title
            xyChart = {
              dataSets = [for ts in try(local.xy[k].multi, [local.xy[k].ts]) : {
                plotType        = local.xy[k].plot, targetAxis = "Y1"
                timeSeriesQuery = { timeSeriesFilter = ts }
              }]
              yAxis = { scale = "LINEAR" }
            }
          }
        }],
        [
          {
            xPos = 24, yPos = 32, width = 24, height = 16
            widget = {
              title = "Model spend by step, USD per day"
              xyChart = {
                dataSets = [{
                  plotType = "STACKED_BAR", targetAxis = "Y1"
                  timeSeriesQuery = {
                    prometheusQuery = "sum by (step) (increase(logging_googleapis_com:user_cypress_llm_cost_usd_sum[1d]))"
                  }
                }]
                yAxis = { scale = "LINEAR" }
              }
            }
          },
          {
            xPos = 0, yPos = 48, width = 48, height = 16
            widget = {
              title = "Errors"
              logsPanel = {
                filter        = "severity>=ERROR AND (resource.type=\"cloud_run_revision\" OR resource.type=\"cloud_run_job\")"
                resourceNames = ["projects/${var.project}"]
              }
            }
          },
        ]
      ) : { for k, v in t : k => v if v != 0 }]
    }
  })
}
