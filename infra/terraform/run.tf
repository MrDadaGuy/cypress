# Cloud Run API service and ingest job (same image, different command).

locals {
  run_env = {
    CYPRESS_ENV        = "prod"
    GCP_PROJECT        = var.project
    DATABASE_URL       = "postgresql://cypress@${local.db_ip}:5432/cypress?connect_timeout=10"
    CYPRESS_INGEST_JOB = local.ingest_job
    # The web app streams answers straight from Cloud Run (Firebase Hosting buffers responses).
    CYPRESS_CORS_ORIGINS = "https://lhca-cypress.web.app,https://lhca-cypress.firebaseapp.com"
  }
}

resource "google_cloud_run_v2_service" "api" {
  name                = "cypress-api"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = false

  template {
    service_account                  = google_service_account.run.email
    max_instance_request_concurrency = 40
    timeout                          = "60s"
    scaling {
      min_instance_count = 0
      max_instance_count = 3 # cost ceiling
    }
    vpc_access {
      egress = "PRIVATE_RANGES_ONLY"
      network_interfaces {
        network    = google_compute_network.cypress.id
        subnetwork = google_compute_subnetwork.cypress.id
      }
    }
    containers {
      image = local.placeholder
      resources {
        limits            = { cpu = "1", memory = "512Mi" }
        cpu_idle          = true
        startup_cpu_boost = true
      }
      dynamic "env" {
        for_each = local.run_env
        content {
          name  = env.key
          value = env.value
        }
      }
      env {
        name = "PGPASSWORD"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.db_password.secret_id
            version = "latest"
          }
        }
      }
      dynamic "env" {
        for_each = local.slack_env
        content {
          name  = env.key
          value = env.value
        }
      }
      dynamic "env" {
        for_each = local.slack_secrets
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = env.value
              version = "latest"
            }
          }
        }
      }
      startup_probe {
        http_get { path = "/health" }
        period_seconds    = 3
        failure_threshold = 20
      }
    }
  }

  lifecycle {
    ignore_changes = [template[0].containers[0].image, client, client_version]
  }
  depends_on = [google_secret_manager_secret_iam_member.db_password, google_secret_manager_secret_iam_member.slack,
  google_project_service.apis]
}

# Public: Firebase Hosting rewrites need it. The app itself checks Firebase sign-in and roles.
resource "google_cloud_run_v2_service_iam_member" "public" {
  name     = google_cloud_run_v2_service.api.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}

resource "google_cloud_run_v2_job" "ingest" {
  name                = "cypress-ingest"
  location            = var.region
  deletion_protection = false

  template {
    parallelism = 1
    task_count  = 1
    template {
      service_account = google_service_account.run.email
      timeout         = "3600s"
      max_retries     = 0
      vpc_access {
        egress = "PRIVATE_RANGES_ONLY"
        network_interfaces {
          network    = google_compute_network.cypress.id
          subnetwork = google_compute_subnetwork.cypress.id
        }
      }
      containers {
        image   = local.placeholder
        command = ["python", "-m", "cypress.ingest.worker", "--once"]
        resources {
          limits = { cpu = "1", memory = "1Gi" }
        }
        dynamic "env" {
          for_each = local.run_env
          content {
            name  = env.key
            value = env.value
          }
        }
        env {
          name = "PGPASSWORD"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.db_password.secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [template[0].template[0].containers[0].image, client, client_version]
  }
  depends_on = [google_secret_manager_secret_iam_member.db_password, google_project_service.apis]
}

# The API starts ingest executions.
resource "google_cloud_run_v2_job_iam_member" "api_runs_ingest" {
  name     = google_cloud_run_v2_job.ingest.name
  location = var.region
  role     = "roles/run.invoker"
  member   = google_service_account.run.member
}
