# App Check for the public chat: the web app proves it's the real Cypress site with an invisible
# reCAPTCHA Enterprise score check (free up to 10,000 assessments a month; a token lasts an hour, so
# each visitor costs about one assessment per hour of chatting). The API decides what to do with
# missing tokens: an admin setting (off / monitor / enforce) in Models & spend > Protection.

resource "google_recaptcha_enterprise_key" "web" {
  project      = var.project
  display_name = "Cypress web (App Check)"
  web_settings {
    integration_type  = "SCORE"
    allow_all_domains = false
    allowed_domains   = ["${var.project}.web.app", "${var.project}.firebaseapp.com", "localhost"]
  }
  depends_on = [google_project_service.apis]
}

resource "google_firebase_app_check_recaptcha_enterprise_config" "web" {
  provider   = google-beta
  project    = var.project
  app_id     = google_firebase_web_app.admin.app_id
  site_key   = google_recaptcha_enterprise_key.web.name
  token_ttl  = "3600s"
  depends_on = [google_project_service.apis]
}

# Public by design (it's in every page that uses reCAPTCHA); goes in apps/web/.env.production.
output "recaptcha_site_key" {
  value = google_recaptcha_enterprise_key.web.name
}
