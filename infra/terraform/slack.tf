# Slack over HTTP: Slack -> /api/slack/events -> Cloud Tasks -> /api/slack/work (OIDC-signed).
# The Slack secrets were created by hand from the clipboard; Terraform only grants access.

data "google_project" "this" {}

locals {
  api_url = "https://cypress-api-${data.google_project.this.number}.${var.region}.run.app"
  slack_env = {
    CYPRESS_SLACK_QUEUE = google_cloud_tasks_queue.slack.id
    CYPRESS_EVAL_QUEUE  = google_cloud_tasks_queue.evals.id
    CYPRESS_SELF_URL    = local.api_url
    CYPRESS_RUN_SA      = google_service_account.run.email
    CYPRESS_TICKETS_URL = local.tickets_url
    CYPRESS_UPLOADS     = "${var.project}-uploads"
  }
  slack_secrets = {
    SLACK_BOT_TOKEN      = "slack-bot-token"
    SLACK_SIGNING_SECRET = "slack-signing-secret"
  }
}

resource "google_cloud_tasks_queue" "slack" {
  name     = "cypress-slack"
  location = var.region
  rate_limits {
    max_dispatches_per_second = 2
    max_concurrent_dispatches = 3 # matches the API's max instances
  }
  retry_config {
    max_attempts = 1 # never post the same answer twice
  }
  depends_on = [google_project_service.apis]
}

# Eval runs: one task per test question, kept off the Slack queue so answers never wait behind them.
resource "google_cloud_tasks_queue" "evals" {
  name     = "cypress-evals"
  location = var.region
  rate_limits {
    max_dispatches_per_second = 1
    max_concurrent_dispatches = 2
  }
  retry_config {
    max_attempts = 2
  }
  depends_on = [google_project_service.apis]
}

resource "google_cloud_tasks_queue_iam_member" "run_enqueues_evals" {
  name     = google_cloud_tasks_queue.evals.name
  location = var.region
  role     = "roles/cloudtasks.enqueuer"
  member   = google_service_account.run.member
}

resource "google_cloud_tasks_queue_iam_member" "run_enqueues" {
  name     = google_cloud_tasks_queue.slack.name
  location = var.region
  role     = "roles/cloudtasks.enqueuer"
  member   = google_service_account.run.member
}

# Lets the API ask Cloud Tasks to sign requests as the API's own identity.
resource "google_service_account_iam_member" "run_acts_as_self" {
  service_account_id = google_service_account.run.name
  role               = "roles/iam.serviceAccountUser"
  member             = google_service_account.run.member
}

resource "google_secret_manager_secret_iam_member" "slack" {
  for_each  = toset(values(local.slack_secrets))
  secret_id = "projects/${var.project}/secrets/${each.key}"
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.run.member
}

output "slack_request_url" {
  value = "${local.api_url}/api/slack/events"
}
