# Firebase: Auth (Google sign-in) and Hosting for the admin UI.
# One manual step: Firebase console > Authentication > Sign-in method > Google > Enable.

resource "google_firebase_project" "default" {
  provider   = google-beta
  project    = var.project
  depends_on = [google_project_service.apis]
}

resource "google_firebase_web_app" "admin" {
  provider        = google-beta
  project         = var.project
  display_name    = "Cypress admin"
  deletion_policy = "DELETE"
  depends_on      = [google_firebase_project.default]
}

resource "google_firebase_hosting_site" "default" {
  provider = google-beta
  project  = var.project
  site_id  = var.project
  app_id   = google_firebase_web_app.admin.app_id
}

output "admin_url" {
  value = "https://${google_firebase_hosting_site.default.site_id}.web.app/admin"
}
output "api_url" {
  value = google_cloud_run_v2_service.api.uri
}
output "db_ip" {
  value = local.db_ip
}
