# Rust tickets service: private (only the API may invoke it), same database as the API.

resource "google_service_account" "tickets" {
  account_id   = "cypress-tickets"
  display_name = "Cypress tickets service"
}

resource "google_secret_manager_secret_iam_member" "tickets_db_password" {
  secret_id = google_secret_manager_secret.db_password.id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.tickets.member
}

locals {
  tickets_url = "https://cypress-tickets-${data.google_project.this.number}.${var.region}.run.app"
}

resource "google_cloud_run_v2_service" "tickets" {
  name                = "cypress-tickets"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL" # reached over its run.app URL; IAM below is the gate
  deletion_protection = false

  template {
    service_account                  = google_service_account.tickets.email
    max_instance_request_concurrency = 40
    timeout                          = "30s"
    scaling {
      min_instance_count = 0
      max_instance_count = 2
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
        limits   = { cpu = "1", memory = "256Mi" }
        cpu_idle = true
      }
      env {
        name  = "DATABASE_URL"
        value = local.run_env.DATABASE_URL
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

  lifecycle {
    ignore_changes = [template[0].containers[0].image, client, client_version]
  }
  depends_on = [google_secret_manager_secret_iam_member.tickets_db_password, google_project_service.apis]
}

# Only the API can call it. No allUsers.
resource "google_cloud_run_v2_service_iam_member" "api_calls_tickets" {
  name     = google_cloud_run_v2_service.tickets.name
  location = var.region
  role     = "roles/run.invoker"
  member   = google_service_account.run.member
}

output "tickets_url" {
  value = local.tickets_url
}
