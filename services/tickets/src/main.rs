//! cypress-tickets: issue tickets for the Lake Hills Community Association.
//!
//! A small private HTTP service on Cloud Run. Only the Cypress API may call it (Cloud Run IAM:
//! no public invoker). The API passes the caller's verified role in `x-cypress-role` and their
//! identity in `x-cypress-actor`; this service re-checks permissions with them (defense in depth),
//! and never trusts identity found in request bodies.
//!
//!   POST  /tickets                 anyone (via the API)       file a report
//!   GET   /tickets?status=open     staff, security, board     list
//!   GET   /tickets/{id}            staff, security, board     ticket + history
//!   PATCH /tickets/{id}            staff, security, board     status / priority / assignee
//!   POST  /tickets/{id}/comments   staff, security, board     add a note
//!   GET   /health

mod error;
mod tickets;

use std::{env, net::SocketAddr, str::FromStr, time::Duration};

use axum::{Router, extract::Request, middleware::Next, response::Response, routing::get};
use sqlx::postgres::{PgConnectOptions, PgPoolOptions};
use tracing_subscriber::{EnvFilter, fmt};

#[derive(Clone)]
pub struct AppState {
    pub db: sqlx::PgPool,
}

fn connect_options() -> Result<PgConnectOptions, sqlx::Error> {
    // Same DATABASE_URL as the Python services; drop libpq-only query params like connect_timeout.
    let url = env::var("DATABASE_URL").unwrap_or_else(|_| "postgres://localhost/cypress".into());
    let base = url
        .split('?')
        .next()
        .unwrap_or(&url)
        .replace("postgresql://", "postgres://");
    let mut opts = PgConnectOptions::from_str(&base)?.application_name("cypress-tickets");
    if let Ok(pw) = env::var("PGPASSWORD") {
        opts = opts.password(&pw);
    }
    Ok(opts)
}

pub fn app(state: AppState) -> Router {
    Router::new()
        .route("/health", get(|| async { "ok" }))
        .merge(tickets::routes())
        .layer(axum::middleware::from_fn(log_request))
        .with_state(state)
}

/// The trace id from the caller's W3C `traceparent` header (`00-<trace>-<span>-<flags>`).
pub fn trace_id(traceparent: Option<&str>) -> Option<String> {
    let mut parts = traceparent?.split('-');
    let (_ver, trace) = (parts.next()?, parts.next()?);
    (trace.len() == 32 && trace.bytes().all(|b| b.is_ascii_hexdigit()) && trace != "0".repeat(32))
        .then(|| trace.to_ascii_lowercase())
}

/// One log line per request. With the caller's trace id at the top level, Cloud Logging files the
/// line under the API's trace, so a chat turn's ticket call shows up beside its trace.
async fn log_request(req: Request, next: Next) -> Response {
    let started = std::time::Instant::now();
    let (method, path) = (req.method().clone(), req.uri().path().to_owned());
    let trace = trace_id(req.headers().get("traceparent").and_then(|v| v.to_str().ok()))
        .map(|t| format!("projects/{}/traces/{t}", env::var("GOOGLE_CLOUD_PROJECT").unwrap_or_else(|_| "lhca-cypress".into())));
    let res = next.run(req).await;
    if path != "/health" {
        tracing::info!(
            "logging.googleapis.com/trace" = trace.as_deref().unwrap_or(""),
            method = %method, path = %path, status = res.status().as_u16(),
            latency_ms = started.elapsed().as_millis() as u64,
            "request"
        );
    }
    res
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    // JSON logs on Cloud Run (picked up by Cloud Logging), readable logs locally.
    let filter =
        EnvFilter::try_from_default_env().unwrap_or_else(|_| "info,tower_http=info".into());
    if env::var("K_SERVICE").is_ok() {
        // Flattened so the trace field sits at the top level, where Cloud Logging looks for it.
        fmt().json().flatten_event(true).with_env_filter(filter).init();
    } else {
        fmt().with_env_filter(filter).init();
    }

    let db = PgPoolOptions::new()
        .max_connections(5) // the e2-micro Postgres is small; Cloud Run max 2 instances
        .acquire_timeout(Duration::from_secs(10))
        .connect_with(connect_options()?)
        .await?;

    let port: u16 = env::var("PORT")
        .ok()
        .and_then(|p| p.parse().ok())
        .unwrap_or(8080);
    let addr = SocketAddr::from(([0, 0, 0, 0], port));
    let listener = tokio::net::TcpListener::bind(addr).await?;
    tracing::info!(%addr, "cypress-tickets listening");
    axum::serve(listener, app(AppState { db }))
        .with_graceful_shutdown(async {
            let _ = tokio::signal::ctrl_c().await;
        })
        .await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::trace_id;

    #[test]
    fn reads_trace_id_from_traceparent() {
        let tp = "00-4BF92F3577B34DA6A3CE929D0E0E4736-00f067aa0ba902b7-01";
        assert_eq!(trace_id(Some(tp)).as_deref(), Some("4bf92f3577b34da6a3ce929d0e0e4736"));
        assert_eq!(trace_id(Some("00-00000000000000000000000000000000-00f067aa0ba902b7-01")), None);
        assert_eq!(trace_id(Some("garbage")), None);
        assert_eq!(trace_id(None), None);
    }
}
