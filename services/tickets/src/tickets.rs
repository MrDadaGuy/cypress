use axum::{
    Json, Router,
    extract::{FromRequestParts, Path, Query, State},
    http::{StatusCode, request::Parts},
    routing::{get, post},
};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::{
    AppState,
    error::{ApiError, ApiResult},
};

pub const STATUSES: &[&str] = &["new", "acknowledged", "in_progress", "resolved", "closed"];
pub const CATEGORIES: &[&str] = &["maintenance", "safety", "rules", "pool", "grounds", "security", "other"];
pub const PRIORITIES: &[&str] = &["low", "normal", "high", "urgent"];
const STAFF_ROLES: &[&str] = &["staff", "security", "board", "admin"];

pub fn routes() -> Router<AppState> {
    Router::new()
        .route("/tickets", post(create).get(list))
        .route("/tickets/{id}", get(fetch).patch(update))
        .route("/tickets/{id}/comments", post(comment))
}

// ---------------- caller ----------------

/// Who is calling, as verified by the Cypress API (which alone can reach this service).
#[derive(Debug, Clone)]
pub struct Caller {
    pub role: Option<String>,
    pub actor: String,
}

impl Caller {
    fn is_staff(&self) -> bool {
        self.role
            .as_deref()
            .is_some_and(|r| STAFF_ROLES.contains(&r))
    }
    fn require_staff(&self) -> ApiResult<()> {
        if self.is_staff() {
            Ok(())
        } else {
            Err(ApiError::Forbidden)
        }
    }
}

impl<S: Send + Sync> FromRequestParts<S> for Caller {
    type Rejection = ApiError;

    async fn from_request_parts(parts: &mut Parts, _: &S) -> Result<Self, Self::Rejection> {
        let header = |name: &str| {
            parts
                .headers
                .get(name)
                .and_then(|v| v.to_str().ok())
                .map(str::trim)
                .filter(|v| !v.is_empty())
        };
        let role = header("x-cypress-role").map(str::to_owned);
        let actor = header("x-cypress-actor")
            .unwrap_or("anonymous")
            .chars()
            .take(200)
            .collect();
        Ok(Caller { role, actor })
    }
}

// ---------------- model ----------------

#[derive(Debug, Serialize, sqlx::FromRow)]
pub struct Ticket {
    pub id: i64,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
    pub status: String,
    pub category: String,
    pub priority: String,
    pub title: String,
    pub description: String,
    pub location: Option<String>,
    pub channel: String,
    pub reporter_role: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reporter_contact: Option<String>,
    pub assignee: Option<String>,
    pub slack_channel: Option<String>,
    pub slack_ts: Option<String>,
}

/// Ticket columns, spliced in at compile time with concat! so every query stays a static string.
macro_rules! cols {
    () => {
        "id, created_at, updated_at, status, category, priority, title, description, location, \
         channel, reporter_role, reporter_contact, assignee, slack_channel, slack_ts"
    };
}

#[derive(Debug, Serialize, sqlx::FromRow)]
pub struct Event {
    pub at: DateTime<Utc>,
    pub actor: String,
    pub kind: String,
    pub body: Option<String>,
    pub data: Value,
}

fn check<'a>(field: &str, value: &'a str, allowed: &[&str]) -> ApiResult<&'a str> {
    if allowed.contains(&value) {
        Ok(value)
    } else {
        Err(ApiError::BadRequest(format!(
            "{field} must be one of: {}",
            allowed.join(", ")
        )))
    }
}

fn text(field: &str, value: &str, min: usize, max: usize) -> ApiResult<String> {
    let v = value.trim();
    let n = v.chars().count();
    if n < min || n > max {
        return Err(ApiError::BadRequest(format!(
            "{field} must be {min}-{max} characters"
        )));
    }
    Ok(v.to_owned())
}

fn opt_text(field: &str, value: &Option<String>, max: usize) -> ApiResult<Option<String>> {
    match value.as_deref().map(str::trim).filter(|v| !v.is_empty()) {
        None => Ok(None),
        Some(v) => text(field, v, 1, max).map(Some),
    }
}

// ---------------- create ----------------

#[derive(Debug, Deserialize)]
pub struct NewTicket {
    pub title: String,
    pub description: String,
    #[serde(default = "default_category")]
    pub category: String,
    #[serde(default = "default_priority")]
    pub priority: String,
    pub location: Option<String>,
    pub channel: String,
    pub reporter_uid: Option<String>,
    pub reporter_contact: Option<String>,
}
fn default_category() -> String {
    "other".into()
}
fn default_priority() -> String {
    "normal".into()
}

async fn create(
    State(st): State<AppState>,
    caller: Caller,
    Json(t): Json<NewTicket>,
) -> ApiResult<(StatusCode, Json<Ticket>)> {
    let title = text("title", &t.title, 3, 200)?;
    let description = text("description", &t.description, 1, 4000)?;
    let category = check("category", &t.category, CATEGORIES)?;
    let priority = check("priority", &t.priority, PRIORITIES)?;
    let location = opt_text("location", &t.location, 300)?;
    let contact = opt_text("reporter_contact", &t.reporter_contact, 200)?;
    let channel = check("channel", &t.channel, &["web", "slack", "console", "email"])?;

    let mut tx = st.db.begin().await?;
    let ticket: Ticket = sqlx::query_as(concat!("insert into tickets (title, description, category, priority, location, channel, reporter_role,
                              reporter_uid, reporter_contact)
         values ($1, $2, $3, $4, $5, $6, $7, $8, $9) returning ", cols!(), ""))
    .bind(&title).bind(&description).bind(category).bind(priority).bind(&location).bind(channel)
    .bind(&caller.role).bind(&t.reporter_uid).bind(&contact)
    .fetch_one(&mut *tx)
    .await?;
    sqlx::query(
        "insert into ticket_events (ticket_id, actor, kind, data) values ($1, $2, 'created', $3)",
    )
    .bind(ticket.id)
    .bind(&caller.actor)
    .bind(json!({ "channel": channel, "priority": priority }))
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;
    tracing::info!(
        id = ticket.id,
        category,
        priority,
        channel,
        "ticket created"
    );
    Ok((StatusCode::CREATED, Json(redact(ticket, &caller))))
}

/// Reporter contact details are only for staff and up.
fn redact(mut t: Ticket, caller: &Caller) -> Ticket {
    if !caller.is_staff() {
        t.reporter_contact = None;
    }
    t
}

// ---------------- read ----------------

#[derive(Debug, Deserialize)]
pub struct ListQuery {
    /// "open" (default: new, acknowledged, in_progress), "all", or one status.
    pub status: Option<String>,
    pub limit: Option<i64>,
}

async fn list(
    State(st): State<AppState>,
    caller: Caller,
    Query(q): Query<ListQuery>,
) -> ApiResult<Json<Vec<Ticket>>> {
    caller.require_staff()?;
    let limit = q.limit.unwrap_or(50).clamp(1, 200);
    let status = q.status.as_deref().unwrap_or("open");
    let statuses: Vec<&str> = match status {
        "open" => vec!["new", "acknowledged", "in_progress"],
        "all" => STATUSES.to_vec(),
        s => vec![check("status", s, STATUSES)?],
    };
    let rows: Vec<Ticket> = sqlx::query_as(concat!(
        "select ",
        cols!(),
        " from tickets where status = any($1)
         order by (priority = 'urgent') desc, (priority = 'high') desc, created_at desc limit $2"
    ))
    .bind(&statuses)
    .bind(limit)
    .fetch_all(&st.db)
    .await?;
    Ok(Json(rows))
}

#[derive(Debug, Serialize)]
pub struct TicketDetail {
    #[serde(flatten)]
    pub ticket: Ticket,
    pub events: Vec<Event>,
}

async fn load(db: &sqlx::PgPool, id: i64) -> ApiResult<Ticket> {
    sqlx::query_as(concat!("select ", cols!(), " from tickets where id = $1"))
        .bind(id)
        .fetch_optional(db)
        .await?
        .ok_or(ApiError::NotFound)
}

async fn detail(db: &sqlx::PgPool, id: i64) -> ApiResult<TicketDetail> {
    let ticket = load(db, id).await?;
    let events = sqlx::query_as("select at, actor, kind, body, data from ticket_events where ticket_id = $1 order by at, id")
        .bind(id)
        .fetch_all(db)
        .await?;
    Ok(TicketDetail { ticket, events })
}

async fn fetch(
    State(st): State<AppState>,
    caller: Caller,
    Path(id): Path<i64>,
) -> ApiResult<Json<TicketDetail>> {
    caller.require_staff()?;
    Ok(Json(detail(&st.db, id).await?))
}

// ---------------- update ----------------

#[derive(Debug, Deserialize)]
pub struct Patch {
    pub status: Option<String>,
    pub priority: Option<String>,
    /// Some("") clears the assignee.
    pub assignee: Option<String>,
    pub note: Option<String>,
    /// Integration bookkeeping (where the Slack notification lives).
    pub slack_channel: Option<String>,
    pub slack_ts: Option<String>,
}

async fn update(
    State(st): State<AppState>,
    caller: Caller,
    Path(id): Path<i64>,
    Json(p): Json<Patch>,
) -> ApiResult<Json<TicketDetail>> {
    caller.require_staff()?;
    let note = opt_text("note", &p.note, 4000)?;
    let mut tx = st.db.begin().await?;
    let cur: Ticket = sqlx::query_as(concat!(
        "select ",
        cols!(),
        " from tickets where id = $1 for update"
    ))
    .bind(id)
    .fetch_optional(&mut *tx)
    .await?
    .ok_or(ApiError::NotFound)?;

    let mut events: Vec<(&str, Option<String>, Value)> = Vec::new();
    if let Some(s) = p.status.as_deref() {
        let s = check("status", s, STATUSES)?;
        if s != cur.status {
            events.push((
                "status",
                note.clone(),
                json!({ "from": cur.status, "to": s }),
            ));
        }
    }
    if let Some(pr) = p.priority.as_deref() {
        let pr = check("priority", pr, PRIORITIES)?;
        if pr != cur.priority {
            events.push(("priority", None, json!({ "from": cur.priority, "to": pr })));
        }
    }
    let assignee = match p.assignee.as_deref().map(str::trim) {
        None => cur.assignee.clone(),
        Some("") => None,
        Some(a) => Some(text("assignee", a, 1, 200)?),
    };
    if assignee != cur.assignee {
        events.push((
            "assigned",
            None,
            json!({ "from": cur.assignee, "to": assignee }),
        ));
    }
    if events.is_empty() && note.is_some() {
        events.push(("comment", note.clone(), json!({})));
    }

    sqlx::query(
        "update tickets set status = coalesce($2, status), priority = coalesce($3, priority), assignee = $4,
                slack_channel = coalesce($5, slack_channel), slack_ts = coalesce($6, slack_ts), updated_at = now()
         where id = $1",
    )
    .bind(id).bind(&p.status).bind(&p.priority).bind(&assignee).bind(&p.slack_channel).bind(&p.slack_ts)
    .execute(&mut *tx)
    .await?;
    for (kind, body, data) in events {
        sqlx::query("insert into ticket_events (ticket_id, actor, kind, body, data) values ($1, $2, $3, $4, $5)")
            .bind(id).bind(&caller.actor).bind(kind).bind(body).bind(data)
            .execute(&mut *tx)
            .await?;
    }
    tx.commit().await?;
    Ok(Json(detail(&st.db, id).await?))
}

#[derive(Debug, Deserialize)]
pub struct NewComment {
    pub body: String,
}

async fn comment(
    State(st): State<AppState>,
    caller: Caller,
    Path(id): Path<i64>,
    Json(c): Json<NewComment>,
) -> ApiResult<(StatusCode, Json<TicketDetail>)> {
    caller.require_staff()?;
    let body = text("body", &c.body, 1, 4000)?;
    load(&st.db, id).await?;
    sqlx::query(
        "insert into ticket_events (ticket_id, actor, kind, body) values ($1, $2, 'comment', $3)",
    )
    .bind(id)
    .bind(&caller.actor)
    .bind(&body)
    .execute(&st.db)
    .await?;
    sqlx::query("update tickets set updated_at = now() where id = $1")
        .bind(id)
        .execute(&st.db)
        .await?;
    Ok((StatusCode::CREATED, Json(detail(&st.db, id).await?)))
}
