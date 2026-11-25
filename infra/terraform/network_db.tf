# Private network and the Postgres VM.

resource "google_compute_network" "cypress" {
  name                    = "cypress"
  auto_create_subnetworks = false
  depends_on              = [google_project_service.apis]
}

resource "google_compute_subnetwork" "cypress" {
  name                     = "cypress-${var.region}"
  network                  = google_compute_network.cypress.id
  region                   = var.region
  ip_cidr_range            = "10.10.0.0/24"
  private_ip_google_access = true # VM reaches Artifact Registry and Secret Manager without a public IP
}

# Postgres is reachable only from inside the subnet (Cloud Run Direct VPC egress uses subnet IPs).
resource "google_compute_firewall" "pg" {
  name          = "cypress-allow-postgres"
  network       = google_compute_network.cypress.id
  source_ranges = [google_compute_subnetwork.cypress.ip_cidr_range]
  target_tags   = ["cypress-db"]
  allow {
    protocol = "tcp"
    ports    = ["5432"]
  }
}

# SSH only through Identity-Aware Proxy: gcloud compute ssh cypress-db --tunnel-through-iap
resource "google_compute_firewall" "iap_ssh" {
  name          = "cypress-allow-iap-ssh"
  network       = google_compute_network.cypress.id
  source_ranges = ["35.235.240.0/20"]
  target_tags   = ["cypress-db"]
  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}

resource "google_compute_address" "db" {
  name         = "cypress-db-internal"
  address_type = "INTERNAL"
  subnetwork   = google_compute_subnetwork.cypress.id
  address      = local.db_ip
  region       = var.region
}

resource "google_compute_instance" "db" {
  name         = "cypress-db"
  machine_type = "e2-micro"
  zone         = var.zone
  tags         = ["cypress-db"]

  boot_disk {
    auto_delete = false # keep the data if the VM is ever recreated
    initialize_params {
      image = "cos-cloud/cos-stable"
      size  = 30 # the free tier covers 30 GB of standard disk
      type  = "pd-standard"
    }
  }

  network_interface {
    subnetwork = google_compute_subnetwork.cypress.id
    network_ip = google_compute_address.db.address
    # no access_config: no public IP
  }

  service_account {
    email  = google_service_account.db.email
    scopes = ["cloud-platform"]
  }

  metadata = {
    user-data                 = templatefile("${path.module}/pg-cloud-init.yaml", { project = var.project, region = var.region, image = local.pg_image })
    google-logging-enabled    = "true"
    google-monitoring-enabled = "true"
    enable-oslogin            = "TRUE"
  }

  shielded_instance_config {
    enable_secure_boot = true
  }

  allow_stopping_for_update = true

  lifecycle {
    ignore_changes = [boot_disk[0].initialize_params[0].image] # COS updates itself
  }

  depends_on = [google_secret_manager_secret_iam_member.db_password, google_artifact_registry_repository_iam_member.db_pull]
}

resource "google_compute_resource_policy" "daily_snapshot" {
  name   = "cypress-db-daily"
  region = var.region
  snapshot_schedule_policy {
    schedule {
      daily_schedule {
        days_in_cycle = 1
        start_time    = "09:00" # UTC, ~3-4 am in Austin
      }
    }
    retention_policy {
      max_retention_days    = 7
      on_source_disk_delete = "KEEP_AUTO_SNAPSHOTS"
    }
    snapshot_properties {
      storage_locations = [var.region]
    }
  }
}

resource "google_compute_disk_resource_policy_attachment" "db" {
  name = google_compute_resource_policy.daily_snapshot.name
  disk = google_compute_instance.db.name
  zone = var.zone
}
