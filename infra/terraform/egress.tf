# A fixed outbound IP for partner allow-lists (Vantaca requires one). Traffic leaves through Cloud NAT
# with this address only from workloads that send all egress through the VPC (the Vantaca sync job,
# when it's added, uses egress = ALL_TRAFFIC). The API and other services keep PRIVATE_RANGES_ONLY,
# so their internet traffic (Vertex, Slack) doesn't go through NAT. ~$4-5/month (address + NAT).

resource "google_compute_address" "egress" {
  name         = "cypress-egress"
  region       = var.region
  address_type = "EXTERNAL"
  description  = "Fixed outbound IP for partner allow-lists (Vantaca)"
}

resource "google_compute_router" "egress" {
  name    = "cypress-egress"
  region  = var.region
  network = google_compute_network.cypress.id
}

resource "google_compute_router_nat" "egress" {
  name                               = "cypress-egress"
  router                             = google_compute_router.egress.name
  region                             = var.region
  nat_ip_allocate_option             = "MANUAL_ONLY"
  nat_ips                            = [google_compute_address.egress.self_link]
  source_subnetwork_ip_ranges_to_nat = "LIST_OF_SUBNETWORKS"
  subnetwork {
    name                    = google_compute_subnetwork.cypress.id
    source_ip_ranges_to_nat = ["ALL_IP_RANGES"]
  }
  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}

output "egress_ip" {
  value       = google_compute_address.egress.address
  description = "Give this to partners (e.g. Vantaca) for their IP allow-list"
}
