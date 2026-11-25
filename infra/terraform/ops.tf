# Scheduled jobs and file storage.

# Morning ticket digest in the board channel. Cloud Scheduler signs the call as the API's own
# service account; the API only accepts that identity on /api/internal/*.
resource "google_cloud_scheduler_job" "digest" {
  name      = "cypress-daily-digest"
  region    = var.region
  schedule  = "0 8 * * *"
  time_zone = "America/Chicago"
  http_target {
    uri         = "${local.api_url}/api/internal/digest"
    http_method = "POST"
    oidc_token {
      service_account_email = google_service_account.run.email
      audience              = local.api_url
    }
  }
  retry_config {
    retry_count = 1
  }
  depends_on = [google_project_service.apis]
}

# Nightly job: re-crawls every source whose Daily/Weekly schedule is due, then the retention cleanup (with
# legal holds respected). Runs before the 8 AM digest, which reports both. The time matches RUN_AT in recrawl.py.
resource "google_cloud_scheduler_job" "recrawl" {
  name      = "cypress-nightly-recrawl"
  region    = var.region
  schedule  = "30 3 * * *"
  time_zone = "America/Chicago"
  http_target {
    uri         = "${local.api_url}/api/internal/nightly"
    http_method = "POST"
    oidc_token {
      service_account_email = google_service_account.run.email
      audience              = local.api_url
    }
  }
}

# Photos and videos attached to reports. Private: photos are served through the API, videos through
# short-lived signed links, to staff and the board only. Videos upload straight from the browser with a
# one-time signed link (too big for an API request), hence the CORS rule for the chat's origin.
resource "google_storage_bucket" "uploads" {
  name                        = "${var.project}-uploads"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  lifecycle_rule {
    condition { age = 730 } # two years
    action { type = "Delete" }
  }
  cors {
    origin          = ["https://lhca-cypress.web.app", "https://lhca-cypress.firebaseapp.com"]
    method          = ["PUT", "GET"]
    response_header = ["Content-Type"]
    max_age_seconds = 3600
  }
  depends_on = [google_project_service.apis]
}

# The API signs those upload/view links as itself (signBlob on its own service account).
resource "google_service_account_iam_member" "run_signs_urls" {
  service_account_id = google_service_account.run.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = google_service_account.run.member
}

resource "google_storage_bucket_iam_member" "run_uploads" {
  bucket = google_storage_bucket.uploads.name
  role   = "roles/storage.objectUser"
  member = google_service_account.run.member
}

# Cloud Scheduler's service agent mints the OIDC token for the digest call.
resource "google_service_account_iam_member" "scheduler_token" {
  service_account_id = google_service_account.run.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-cloudscheduler.iam.gserviceaccount.com"
}

# Keep the API warm during waking hours so the first Slack message or chat after a quiet spell doesn't wait for a
# cold start (~15 s). Cloud Run instances idle out after ~15 minutes; a ping every 10 minutes keeps one alive.
# With request-based billing an idle instance costs nothing, and each ping uses ~10 ms of CPU: effectively free
# (Cloud Scheduler's free tier covers 3 jobs; this is the third). Overnight it scales to zero as before.
resource "google_cloud_scheduler_job" "warm" {
  name      = "cypress-keep-warm"
  region    = var.region
  schedule  = "*/10 6-22 * * *"
  time_zone = "America/Chicago"
  http_target {
    uri         = "${local.api_url}/health"
    http_method = "GET"
  }
  retry_config {
    retry_count = 0
  }
  depends_on = [google_project_service.apis]
}
