# Cypress on GCP, kept near the free tier:
#   - Postgres 17 + pgvector in a container on an always-free e2-micro (no public IP)
#   - FastAPI on Cloud Run (scale to zero) reaching the VM over Direct VPC egress
#   - Ingest worker as a Cloud Run Job, started by the API when work is queued
#   - Admin UI on Firebase Hosting, /api/** rewritten to Cloud Run (same origin)
#
# Images are deployed by infra/deploy.sh; Terraform ignores image changes after creation.

terraform {
  required_version = ">= 1.9"
  backend "gcs" {
    bucket = "lhca-cypress-tfstate"
    prefix = "cypress"
  }
  required_providers {
    google      = { source = "hashicorp/google", version = "~> 7.0" }
    google-beta = { source = "hashicorp/google-beta", version = "~> 7.0" }
    random      = { source = "hashicorp/random", version = "~> 3.6" }
  }
}

variable "project" {
  type    = string
  default = "lhca-cypress"
}
variable "region" {
  type    = string
  default = "us-central1" # e2-micro is only free in us-central1, us-east1, us-west1
}
variable "zone" {
  type    = string
  default = "us-central1-a"
}

locals {
  db_ip       = "10.10.0.10"
  placeholder = "us-docker.pkg.dev/cloudrun/container/hello"
  pg_image    = "${var.region}-docker.pkg.dev/${var.project}/dockerhub/pgvector/pgvector:0.8.1-pg17"
  ingest_job  = "projects/${var.project}/locations/${var.region}/jobs/cypress-ingest"
}

provider "google" {
  project               = var.project
  region                = var.region
  billing_project       = var.project
  user_project_override = true
}
provider "google-beta" {
  project               = var.project
  region                = var.region
  billing_project       = var.project
  user_project_override = true
}

resource "google_project_service" "apis" {
  for_each = toset([
    "compute.googleapis.com", "run.googleapis.com", "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com", "secretmanager.googleapis.com", "aiplatform.googleapis.com",
    "firebase.googleapis.com", "firebasehosting.googleapis.com", "identitytoolkit.googleapis.com",
    "iap.googleapis.com", "cloudresourcemanager.googleapis.com", "serviceusage.googleapis.com",
    "cloudtasks.googleapis.com", "cloudscheduler.googleapis.com", "storage.googleapis.com",
    "cloudtrace.googleapis.com", "monitoring.googleapis.com", "observability.googleapis.com", "sheets.googleapis.com", "drive.googleapis.com", "calendar-json.googleapis.com",
    "recaptchaenterprise.googleapis.com", "firebaseappcheck.googleapis.com",
  ])
  service            = each.key
  disable_on_destroy = false
}

# ---------------- registry ----------------

resource "google_artifact_registry_repository" "cypress" {
  repository_id = "cypress"
  location      = var.region
  format        = "DOCKER"
  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions { keep_count = 5 }
  }
  cleanup_policies {
    id     = "delete-old"
    action = "DELETE"
    condition { older_than = "2592000s" }
  }
  depends_on = [google_project_service.apis]
}

# Docker Hub proxy, so the VM can pull the Postgres image without internet access.
resource "google_artifact_registry_repository" "dockerhub" {
  repository_id = "dockerhub"
  location      = var.region
  format        = "DOCKER"
  mode          = "REMOTE_REPOSITORY"
  remote_repository_config {
    docker_repository { public_repository = "DOCKER_HUB" }
  }
  depends_on = [google_project_service.apis]
}

# ---------------- service accounts ----------------

resource "google_service_account" "run" {
  account_id   = "cypress-run"
  display_name = "Cypress API and ingest (Cloud Run)"
}

resource "google_service_account" "db" {
  account_id   = "cypress-db"
  display_name = "Cypress Postgres VM"
}

resource "google_project_iam_member" "run_roles" {
  for_each = toset(["roles/aiplatform.user", "roles/logging.logWriter", "roles/cloudtrace.agent"])
  project  = var.project
  role     = each.key
  member   = google_service_account.run.member
}

resource "google_project_iam_member" "db_roles" {
  for_each = toset(["roles/logging.logWriter", "roles/monitoring.metricWriter"])
  project  = var.project
  role     = each.key
  member   = google_service_account.db.member
}

resource "google_artifact_registry_repository_iam_member" "db_pull" {
  location   = var.region
  repository = google_artifact_registry_repository.dockerhub.name
  role       = "roles/artifactregistry.reader"
  member     = google_service_account.db.member
}

# ---------------- database password ----------------

resource "random_password" "db" {
  length  = 32
  special = false
}

resource "google_secret_manager_secret" "db_password" {
  secret_id = "db-password"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "db_password" {
  secret      = google_secret_manager_secret.db_password.id
  secret_data = random_password.db.result
}

resource "google_secret_manager_secret_iam_member" "db_password" {
  for_each  = { run = google_service_account.run.member, db = google_service_account.db.member }
  secret_id = google_secret_manager_secret.db_password.id
  role      = "roles/secretmanager.secretAccessor"
  member    = each.value
}
