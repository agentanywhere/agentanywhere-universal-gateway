//! Request handling: serve what the data plane owns, hand the rest to the control plane.
//!
//! A request is handled natively only when everything that decides its outcome is something
//! this phase reproduces exactly: a plain HTTP path, a key present in the snapshot that is
//! active, unexpired and without tenant quota, IP rules or policies, and a client that is not
//! a browser. Every other request is forwarded unchanged to the control plane, so behaviour
//! never differs; only speed does. See DESIGN.md.
use bytes::Bytes;
use http_body_util::{combinators::BoxBody, BodyExt, Full};
use hyper::body::Incoming;
use hyper::header::{HeaderName, HeaderValue, ACCEPT, CONTENT_TYPE, HOST};
use hyper::{HeaderMap, Request, Response, StatusCode, Uri};
use hyper_util::client::legacy::{connect::HttpConnector, Client};
use std::convert::Infallible;
use std::net::SocketAddr;
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use crate::snapshot::{hash_key, Snapshot};
use crate::report::{Reporter, Row};
use crate::sync::Link;
use std::time::Instant;

pub type Body = BoxBody<Bytes, hyper::Error>;

pub struct State {
    pub snapshot: arc_swap::ArcSwap<Snapshot>,
    pub backend: String,
    pub control: String,
    pub client: Client<HttpConnector, Incoming>,
    /// Present when the snapshot comes from the control plane; native serving needs it fresh.
    pub sync: Option<Arc<Link>>,
    /// Present with a control-plane link: what was served is reported back.
    pub reporter: Option<Arc<Reporter>>,
}

const HOP_BY_HOP: [&str; 8] = [
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
];

/// First path segments after `/api/` that belong to other protocols (and so to the control plane).
const OTHER_PROTOCOLS: [&str; 9] = ["mcp", "a2a", "agui", "acp", "mqtt", "ws", "grpc", "graphql", "agents"];

fn is_native_path(path: &str, fallthrough: &[String]) -> bool {
    let Some(rest) = path.strip_prefix("/api/") else { return false };
    let first = rest.split('/').next().unwrap_or("");
    if OTHER_PROTOCOLS.contains(&first) {
        return false;
    }
    !fallthrough.iter().any(|p| path.starts_with(p.as_str()))
}

fn wants_html(headers: &HeaderMap) -> bool {
    headers
        .get(ACCEPT)
        .and_then(|v| v.to_str().ok())
        .map(|v| v.contains("text/html"))
        .unwrap_or(false)
}

fn full(bytes: impl Into<Bytes>) -> Body {
    Full::new(bytes.into()).map_err(|never| match never {}).boxed()
}

fn now_secs() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

/// `YYYY-MM-DDTHH:MM:SS.ffffff` in UTC, the shape the Python gateway puts in error bodies.
fn iso_now() -> String {
    let d = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default();
    let (secs, micros) = (d.as_secs() as i64, d.subsec_micros());
    let (days, rem) = (secs.div_euclid(86_400), secs.rem_euclid(86_400));
    // Days since 1970-01-01 to a civil date (Howard Hinnant's algorithm).
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}.{micros:06}",
        rem / 3600,
        (rem % 3600) / 60,
        rem % 60
    )
}

/// The API version a request resolves to, as the Python gateway does for a path that does not
/// start with `/v<N>`: the `Accept-Version` or `X-API-Version` header (a leading v is ignored),
/// else the default. `None` when there is none; `Some((version, native_ok))` otherwise.
fn resolve_version<'a>(snap: &'a Snapshot, headers: &HeaderMap) -> Option<(&'a str, bool)> {
    let wanted = headers
        .get("accept-version")
        .or_else(|| headers.get("x-api-version"))
        .and_then(|v| v.to_str().ok())
        .map(|v| v.trim().trim_start_matches(['v', 'V']))
        .filter(|v| !v.is_empty())
        .and_then(|v| snap.api_versions.native_ok.get_key_value(v));
    let found = wanted.or_else(|| {
        snap.api_versions.default.as_deref().and_then(|d| snap.api_versions.native_ok.get_key_value(d))
    });
    found.map(|(k, ok)| (k.as_str(), *ok))
}

/// What the reporter needs from a request, captured before the request is consumed.
struct Meta {
    request_id: String,
    method: String,
    path: String,
    user_agent: Option<String>,
    key_id: String,
}

struct Trace {
    request_id: String,
    traceparent: String,
}

/// An incoming `traceparent` is honoured (same trace, new span for this hop); otherwise a new trace starts.
fn trace_for(headers: &HeaderMap) -> Trace {
    let request_id = headers
        .get("x-request-id")
        .and_then(|v| v.to_str().ok())
        .filter(|v| !v.is_empty())
        .map(str::to_owned)
        .unwrap_or_else(|| uuid::Uuid::new_v4().to_string());
    let trace_id = headers
        .get("traceparent")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| {
            let mut it = v.split('-');
            let (_, t, _) = (it.next()?, it.next()?, it.next()?);
            (t.len() == 32 && t.bytes().all(|c| c.is_ascii_hexdigit())).then(|| t.to_owned())
        })
        .unwrap_or_else(|| format!("{:016x}{:016x}", fastrand::u64(..), fastrand::u64(..)));
    Trace { request_id, traceparent: format!("00-{trace_id}-{:016x}-01", fastrand::u64(..)) }
}

fn rate_limited(window: &str, limit: u32, retry_after: u32, t: &Trace) -> Response<Body> {
    let body = format!(
        "{{\"error\":\"RATE_LIMIT_EXCEEDED\",\"message\":\"Rate limit exceeded: {limit} requests per {window}\",\
         \"request_id\":\"{}\",\"timestamp\":\"{}\",\"details\":{{\"limit\":{limit},\"window\":\"{window}\",\"retry_after\":{retry_after}}}}}",
        t.request_id,
        iso_now()
    );
    let mut resp = Response::new(full(body));
    *resp.status_mut() = StatusCode::TOO_MANY_REQUESTS;
    let h = resp.headers_mut();
    h.insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
    h.insert("retry-after", HeaderValue::from(retry_after));
    h.insert("x-content-type-options", HeaderValue::from_static("nosniff"));
    stamp(h, t);
    resp
}

fn bad_gateway(t: &Trace) -> Response<Body> {
    let body = format!(
        "{{\"error\":\"BACKEND_ERROR\",\"message\":\"Backend: upstream request failed\",\"request_id\":\"{}\",\"timestamp\":\"{}\"}}",
        t.request_id,
        iso_now()
    );
    let mut resp = Response::new(full(body));
    *resp.status_mut() = StatusCode::BAD_GATEWAY;
    resp.headers_mut().insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
    stamp(resp.headers_mut(), t);
    resp
}

fn stamp(h: &mut HeaderMap, t: &Trace) {
    if let Ok(v) = HeaderValue::from_str(&t.request_id) {
        h.insert("x-request-id", v);
    }
    if let Ok(v) = HeaderValue::from_str(&t.traceparent) {
        h.insert("traceparent", v);
    }
}

pub async fn handle(
    st: Arc<State>,
    peer: SocketAddr,
    req: Request<Incoming>,
) -> Result<Response<Body>, Infallible> {
    let started = Instant::now();
    // The control plane's internal endpoints are never reachable from clients.
    if req.uri().path().starts_with("/internal/") {
        let mut resp = Response::new(full("{\"detail\":\"Not Found\"}"));
        *resp.status_mut() = StatusCode::NOT_FOUND;
        resp.headers_mut().insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
        return Ok(resp);
    }
    let snap = st.snapshot.load();
    // Fail closed: if the snapshot is not known to be current, the Python gateway decides.
    let trusted = st.sync.as_ref().map(|s| s.fresh()).unwrap_or(true);
    if trusted && is_native_path(req.uri().path(), &snap.fallthrough_prefixes) && !wants_html(req.headers()) {
        if let Some(entry) = req
            .headers()
            .get("x-api-key")
            .and_then(|v| snap.keys.get(&hash_key(v.as_bytes())))
        {
            let now = now_secs();
            let expired = entry.expires_at.map(|e| e <= now as i64).unwrap_or(false);
            let version = resolve_version(&snap, req.headers());
            let version_ok = version.map(|(_, ok)| ok).unwrap_or(true);
            if entry.is_active && !expired && entry.native_ok && version_ok {
                let t = trace_for(req.headers());
                let meta = st.reporter.as_ref().map(|_| Meta {
                    request_id: t.request_id.clone(),
                    method: req.method().as_str().to_owned(),
                    path: req.uri().path().to_owned(),
                    user_agent: req.headers().get("user-agent").and_then(|v| v.to_str().ok()).map(str::to_owned),
                    key_id: entry.id.clone(),
                });
                let denied = if snap.rate_limit_enabled {
                    if !entry.minute.allow(entry.per_minute, 60, now as u32) {
                        Some(rate_limited("minute", entry.per_minute, 60, &t))
                    } else if !entry.hour.allow(entry.per_hour, 3600, now as u32) {
                        Some(rate_limited("hour", entry.per_hour, 3600, &t))
                    } else {
                        None
                    }
                } else {
                    None
                };
                let version = version.map(|(v, _)| v.to_owned());
                let inject_request_id = snap.inject_request_id;
                drop(snap);
                let was_denied = denied.is_some();
                let mut resp = match denied {
                    Some(r) => r,
                    None => forward(&st, &st.backend, peer, req, Some(t), version.as_deref(), inject_request_id).await,
                };
                let h = resp.headers_mut();
                // Tells operators (and the conformance suite) which plane answered.
                h.insert("x-served-by", HeaderValue::from_static("dataplane"));
                if let Ok(v) = HeaderValue::from_str(&format!("{:.3}", started.elapsed().as_secs_f64())) {
                    h.insert("x-response-time", v);
                }
                if let Some(v) = version.as_deref().and_then(|v| HeaderValue::from_str(v).ok()) {
                    h.insert("x-api-version", v);
                }
                if let (Some(reporter), Some(m)) = (&st.reporter, meta) {
                    let status = resp.status().as_u16();
                    let seconds = started.elapsed().as_secs_f64();
                    reporter.record(&m.method, &m.path, status, seconds, &m.key_id, was_denied, || Row {
                        request_id: m.request_id.clone(),
                        method: m.method.clone(),
                        path: m.path.clone(),
                        client_ip: peer.ip().to_string(),
                        api_key_id: Some(m.key_id.clone()),
                        status,
                        duration_ms: seconds * 1000.0,
                        user_agent: m.user_agent.clone(),
                        error: was_denied.then(|| "RATE_LIMIT_EXCEEDED".to_string()),
                    });
                }
                return Ok(resp);
            }
        }
    }
    drop(snap);
    Ok(forward(&st, &st.control, peer, req, None, None, false).await)
}

/// Forward to `base`. With a trace, this is a native request: the Python gateway's
/// upstream headers are reproduced and the response is stamped. Without one, it is handed
/// to the control plane as it came, plus the client's address.
async fn forward(
    st: &State,
    base: &str,
    peer: SocketAddr,
    req: Request<Incoming>,
    native: Option<Trace>,
    version: Option<&str>,
    inject_request_id: bool,
) -> Response<Body> {
    let (parts, body) = req.into_parts();
    let pq = parts.uri.path_and_query().map(|p| p.as_str()).unwrap_or("/");
    let Ok(uri) = format!("{base}{pq}").parse::<Uri>() else {
        return bad_gateway(&native.unwrap_or(Trace { request_id: String::new(), traceparent: String::new() }));
    };
    let mut out = Request::builder().method(parts.method).uri(uri);
    let headers = out.headers_mut().expect("builder has headers");
    for (name, value) in parts.headers.iter() {
        let n = name.as_str();
        if HOP_BY_HOP.contains(&n) || (native.is_some() && (name == HOST || n.starts_with("x-gateway-"))) {
            continue;
        }
        headers.append(name.clone(), value.clone());
    }
    match &native {
        Some(t) => {
            headers.insert("x-protocol", HeaderValue::from_static("http"));
            if let Some(v) = version.and_then(|v| HeaderValue::from_str(v).ok()) {
                headers.insert("x-api-version", v);
            }
            if !parts.headers.contains_key("x-forwarded-for") {
                headers.insert("x-forwarded-for", HeaderValue::from_static(""));
            }
            if let Ok(v) = HeaderValue::from_str(&t.traceparent) {
                headers.insert("traceparent", v);
            }
            if inject_request_id && !parts.headers.contains_key("x-request-id") {
                if let Ok(v) = HeaderValue::from_str(&t.request_id) {
                    headers.insert("x-request-id", v);
                }
            }
        }
        None => {
            let xff = match parts.headers.get("x-forwarded-for").and_then(|v| v.to_str().ok()) {
                Some(prior) => format!("{prior}, {}", peer.ip()),
                None => peer.ip().to_string(),
            };
            if let Ok(v) = HeaderValue::from_str(&xff) {
                headers.insert(HeaderName::from_static("x-forwarded-for"), v);
            }
        }
    }
    let Ok(request) = out.body(body) else {
        return bad_gateway(&native.unwrap_or(Trace { request_id: String::new(), traceparent: String::new() }));
    };
    match st.client.request(request).await {
        Ok(resp) => {
            let (mut rp, rb) = resp.into_parts();
            for h in HOP_BY_HOP {
                rp.headers.remove(h);
            }
            if let Some(t) = &native {
                stamp(&mut rp.headers, t);
            }
            Response::from_parts(rp, rb.boxed())
        }
        Err(_) => bad_gateway(&native.unwrap_or(Trace { request_id: uuid::Uuid::new_v4().to_string(), traceparent: String::new() })),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_plain_http_api_paths_are_native() {
        let none: Vec<String> = vec![];
        assert!(is_native_path("/api/users", &none));
        assert!(is_native_path("/api/v1/orders/7", &none));
        for p in ["/admin/keys", "/oauth2/token", "/health", "/mcp/x", "/api/mcp/x", "/api/a2a", "/api/graphql", "/docs"] {
            assert!(!is_native_path(p, &none), "{p}");
        }
    }

    #[test]
    fn control_plane_prefixes_win() {
        assert!(!is_native_path("/api/custom/x", &["/api/custom".to_string()]));
    }

    #[test]
    fn iso_timestamp_has_the_python_shape() {
        let s = iso_now();
        assert_eq!(s.len(), 26);
        assert_eq!(&s[4..5], "-");
        assert_eq!(&s[10..11], "T");
        assert_eq!(&s[19..20], ".");
    }

    #[test]
    fn an_incoming_trace_is_kept_with_a_new_span() {
        let mut h = HeaderMap::new();
        h.insert("traceparent", HeaderValue::from_static("00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"));
        h.insert("x-request-id", HeaderValue::from_static("req-1"));
        let t = trace_for(&h);
        assert_eq!(t.request_id, "req-1");
        assert!(t.traceparent.starts_with("00-0af7651916cd43dd8448eb211c80319c-"));
        assert!(!t.traceparent.contains("b7ad6b7169203331"));
    }

    #[test]
    fn a_missing_trace_starts_a_new_one() {
        let t = trace_for(&HeaderMap::new());
        assert_eq!(t.request_id.len(), 36);
        assert_eq!(t.traceparent.len(), 55);
    }

    fn snap() -> Snapshot {
        Snapshot::from_json(r#"{"keys":[],"api_versions":{"default":"1","versions":[
            {"version":"1"},{"version":"2"},{"version":"3","native_ok":false}]}}"#).unwrap()
    }

    #[test]
    fn the_default_version_applies_without_a_hint() {
        assert_eq!(resolve_version(&snap(), &HeaderMap::new()), Some(("1", true)));
    }

    #[test]
    fn a_header_picks_the_version_and_ignores_a_leading_v() {
        let mut h = HeaderMap::new();
        h.insert("x-api-version", HeaderValue::from_static("V2"));
        assert_eq!(resolve_version(&snap(), &h), Some(("2", true)));
        let mut h = HeaderMap::new();
        h.insert("accept-version", HeaderValue::from_static("3"));
        assert_eq!(resolve_version(&snap(), &h), Some(("3", false)));
    }

    #[test]
    fn an_unknown_version_hint_falls_back_to_the_default() {
        let mut h = HeaderMap::new();
        h.insert("x-api-version", HeaderValue::from_static("9"));
        assert_eq!(resolve_version(&snap(), &h), Some(("1", true)));
    }

    #[test]
    fn browsers_are_not_native() {
        let mut h = HeaderMap::new();
        h.insert(ACCEPT, HeaderValue::from_static("text/html,application/xhtml+xml"));
        assert!(wants_html(&h));
        assert!(!wants_html(&HeaderMap::new()));
    }
}
