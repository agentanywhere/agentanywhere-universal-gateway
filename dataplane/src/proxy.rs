//! Request handling: serve what the data plane owns, hand the rest to the control plane.
//!
//! A request is handled natively only when everything that decides its outcome is something
//! the data plane reproduces exactly: a path that belongs to plain HTTP, MCP, A2A, AG-UI or ACP,
//! a key present in the snapshot that is active and unexpired, and a client that is not a
//! browser. For such a request the key's allowed paths, the IP rules, the policies, the checks on
//! the body at the protocol boundary, the key's rate limit and its tenant's quota are all
//! decided here, in the Python gateway's order and with its answers. Every other request is
//! forwarded unchanged to the control plane, so behaviour never differs; only speed does.
//! See DESIGN.md.
use bytes::Bytes;
use http_body_util::{BodyExt, Either, Full};
use hyper::body::{Body as _, Incoming};
use hyper::header::{
    HeaderName, HeaderValue, ACCEPT, ACCESS_CONTROL_ALLOW_CREDENTIALS, ACCESS_CONTROL_ALLOW_ORIGIN, AUTHORIZATION, CONNECTION, CONTENT_LENGTH, CONTENT_TYPE, COOKIE,
    HOST, ORIGIN, REFERER, UPGRADE, USER_AGENT, VARY,
};
use hyper::http::uri::{Authority, Scheme};
use hyper::{HeaderMap, Method, Request, Response, StatusCode, Uri, Version};
use hyper_util::client::legacy::{connect::HttpConnector, Client};
use hyper_util::rt::{TokioExecutor, TokioIo};
use std::borrow::Cow;
use std::collections::HashMap;
use std::convert::Infallible;
use std::net::{IpAddr, SocketAddr};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use crate::accesslog::{self, AccessLog};
use crate::bridge;
use crate::boundary::Verdict;
use crate::breaker::{self, Breaker};
use crate::govern::{self, IpRule, Policy, Scope, Subject};
use crate::pyjson;
use crate::report::{Event, Reporter, Row, Served};
use crate::snapshot::{
    change_headers, put, change_query, change_status, read_query, write_query, Notices, hash_key, Actions, AgentFailure, Digest32, DynamicRoute, HeaderLines, KeyEntry, Readiness, Shaping,
    Snapshot, Wording,
};
use crate::sync::Link;

/// A body is either streamed from the other side or held here whole; neither is boxed. Responses
/// are streamed from upstream or made here; requests are streamed from the client, or read whole
/// when the gateway looks inside them.
pub type Body = Either<Incoming, Either<Full<Bytes>, crate::bridge::Lines>>;

/// The protocols served natively. Each has its own backend; which one a path belongs to is
/// decided as the Python gateway's router decides it.
#[derive(Clone, Copy, PartialEq, Debug)]
pub enum Protocol {
    Http,
    Mcp,
    A2a,
    AgUi,
    Acp,
    /// gRPC calls, which arrive over HTTP/2 and are told apart by their content type, not by path.
    Grpc,
    /// `POST /mqtt/publish`, which goes to no backend: the message is published to the broker.
    Mqtt,
    /// Requests under `/graphql`, passed to the GraphQL server as they came.
    GraphQl,
    /// JSON posted under `/grpc`, which the gateway turns into a gRPC call of its own.
    Bridge,
}

/// The protocols whose backends come from the snapshot, in the order `Snapshot::routes` holds them.
pub const ROUTED: [Protocol; 5] = [Protocol::Mcp, Protocol::A2a, Protocol::AgUi, Protocol::Acp, Protocol::GraphQl];

impl Protocol {
    /// The name the Python gateway gives the protocol: the `X-Protocol` header sent upstream and
    /// the key of its backend in the snapshot.
    pub fn name(self) -> &'static str {
        match self {
            Protocol::Http => "http",
            Protocol::Mcp => "mcp",
            Protocol::A2a => "a2a",
            Protocol::AgUi => "ag_ui",
            Protocol::Acp => "acp",
            Protocol::Grpc | Protocol::Bridge => "grpc",
            Protocol::Mqtt => "mqtt",
            Protocol::GraphQl => "graphql",
        }
    }

    fn route_index(self) -> Option<usize> {
        ROUTED.iter().position(|p| *p == self)
    }
}

/// Where requests are sent, parsed once so that no request pays for it.
pub struct Upstream {
    pub text: String,
    scheme: Scheme,
    authority: Authority,
    /// A path the upstream's own URL carries, put before every request's path.
    prefix: String,
    /// An `https://` address: spoken to over TLS.
    tls: bool,
}

/// How backends behind TLS are verified, decided once when the process starts. `None`: this
/// side does not speak TLS to backends, and an `https://` backend is the Python gateway's.
static TLS: OnceLock<Option<Arc<rustls::ClientConfig>>> = OnceLock::new();

/// Whether the certificates backends are verified against come from `SSL_CERT_FILE`.
static TLS_FROM_FILE: OnceLock<bool> = OnceLock::new();

/// Decide how backends behind TLS are verified, as the Python gateway's HTTP client decides it:
/// against the certificates in `SSL_CERT_FILE` when that is set, and otherwise against the
/// Mozilla root certificates (the set that client ships with). With `SSL_CERT_DIR` set, or a
/// file that cannot be read, `https://` backends are left to the Python gateway.
pub fn start_tls() -> Result<(), String> {
    let set = |name: &str| std::env::var_os(name).filter(|v| !v.is_empty());
    let decided = match (set("SSL_CERT_FILE"), set("SSL_CERT_DIR")) {
        (_, Some(_)) => Err("SSL_CERT_DIR is set".to_owned()),
        (Some(file), None) => roots_from(std::path::Path::new(&file)).map(|roots| rustls::ClientConfig::builder().with_root_certificates(roots).with_no_client_auth()),
        (None, None) => {
            let roots = rustls::RootCertStore::from_iter(webpki_roots::TLS_SERVER_ROOTS.iter().cloned());
            Ok(rustls::ClientConfig::builder().with_root_certificates(roots).with_no_client_auth())
        }
    };
    let (config, said) = match decided {
        Ok(config) => (Some(Arc::new(config)), Ok(())),
        Err(why) => (None, Err(why)),
    };
    let _ = TLS_FROM_FILE.set(config.is_some() && set("SSL_CERT_FILE").is_some());
    let _ = TLS.set(config);
    said
}

fn roots_from(file: &std::path::Path) -> Result<rustls::RootCertStore, String> {
    use rustls::pki_types::{pem::PemObject, CertificateDer};
    let mut roots = rustls::RootCertStore::empty();
    for cert in CertificateDer::pem_file_iter(file).map_err(|e| format!("{}: {e}", file.display()))? {
        roots.add(cert.map_err(|e| format!("{}: {e}", file.display()))?).map_err(|e| format!("{}: {e}", file.display()))?;
    }
    if roots.is_empty() {
        return Err(format!("{}: no certificates", file.display()));
    }
    Ok(roots)
}

fn tls() -> Option<&'static Arc<rustls::ClientConfig>> {
    TLS.get().and_then(Option::as_ref)
}

/// Whether a `wss://` backend is this side's to connect to. The Python gateway's WebSocket client
/// verifies against the system's certificates plus `SSL_CERT_FILE`, not the Mozilla set: only
/// with that file given do both sides trust what this side trusts.
fn tls_for_websockets() -> bool {
    TLS_FROM_FILE.get().copied().unwrap_or(false)
}

/// How an `mqtts://` broker is verified, when it is this side's to connect to: as with a
/// `wss://` backend, only against the certificates in `SSL_CERT_FILE`, which the Python gateway's
/// MQTT client reads as well.
pub fn tls_for_brokers() -> Option<Arc<rustls::ClientConfig>> {
    tls().filter(|_| tls_for_websockets()).cloned()
}

impl Upstream {
    pub fn parse(text: &str) -> Result<Upstream, String> {
        let text = text.trim_end_matches('/');
        let uri: Uri = text.parse().map_err(|e| format!("{text}: {e}"))?;
        match (uri.scheme(), uri.authority()) {
            (Some(scheme), Some(authority)) if *scheme == Scheme::HTTP || (*scheme == Scheme::HTTPS && tls().is_some()) => Ok(Upstream {
                text: text.to_owned(),
                scheme: scheme.clone(),
                authority: authority.clone(),
                prefix: uri.path().trim_end_matches('/').to_owned(),
                tls: *scheme == Scheme::HTTPS,
            }),
            _ => Err(format!("{text}: must be an http:// address (or https://, where this side speaks TLS to backends)")),
        }
    }
}

/// Somewhere native requests are sent, with the circuit breaker that protects it. The control
/// plane keeps breakers of its own for what it serves.
pub struct Backend {
    pub upstream: Upstream,
    pub breaker: Breaker,
    /// Nothing is ever sent here: the address is in a scheme the Python gateway's HTTP client
    /// refuses, and every request fails as it fails there.
    pub never_sent: bool,
}

impl Backend {
    pub fn parse(text: &str) -> Result<Arc<Backend>, String> {
        Ok(Arc::new(Backend { upstream: Upstream::parse(text)?, breaker: Breaker::new(), never_sent: false }))
    }

    /// The backend of a dynamic route, under the address the route gives. The Python gateway's
    /// HTTP client speaks HTTP to a `ws://` address and HTTP over TLS to a `wss://` one, so this
    /// side does. An address `never_sent` is only a name for the breaker that counts its failures.
    fn of_route(text: &str, never_sent: bool) -> Result<Arc<Backend>, String> {
        let spoken = match text.split_once("://") {
            _ if never_sent => "http://never.invalid".to_owned(),
            Some(("ws", rest)) => format!("http://{rest}"),
            Some(("wss", rest)) => format!("https://{rest}"),
            _ => text.to_owned(),
        };
        let upstream = Upstream { text: text.to_owned(), ..Upstream::parse(&spoken)? };
        Ok(Arc::new(Backend { upstream, breaker: Breaker::new(), never_sent }))
    }
}

/// Every backend seen so far, by address. Two protocols sent to one address share its breaker,
/// as they do in the Python gateway, and a backend keeps its breaker across snapshots.
#[derive(Default)]
pub struct Backends(Mutex<HashMap<String, Arc<Backend>>>);

impl Backends {
    pub fn with(first: &Arc<Backend>) -> Backends {
        let known = Backends::default();
        known.0.lock().unwrap().insert(first.upstream.text.clone(), first.clone());
        known
    }

    /// The backend at this address, or `None` if it is not one this side can reach (not http://).
    fn at(&self, url: &str) -> Option<Arc<Backend>> {
        let key = url.trim_end_matches('/');
        let mut known = self.0.lock().unwrap();
        if let Some(found) = known.get(key) {
            return Some(found.clone());
        }
        let made = Backend::parse(key).ok()?;
        known.insert(key.to_owned(), made.clone());
        Some(made)
    }

    /// The backend of a dynamic route. Kept by the address the route gives, as the Python gateway
    /// keeps its breakers: `ws://host` and `http://host` are two backends with a breaker each.
    fn of_route(&self, url: &str, never_sent: bool) -> Option<Arc<Backend>> {
        let key = url.trim_end_matches('/');
        let mut known = self.0.lock().unwrap();
        if let Some(found) = known.get(key).filter(|b| b.never_sent == never_sent) {
            return Some(found.clone());
        }
        let made = Backend::of_route(key, never_sent).ok()?;
        known.insert(key.to_owned(), made.clone());
        Some(made)
    }

    /// Give each protocol in the snapshot its backend.
    pub fn resolve(&self, snap: &mut Snapshot) {
        for (route, url) in snap.routes.iter_mut().zip(&snap.protocol_urls) {
            *route = url.as_deref().and_then(|u| self.at(u));
        }
        // A WebSocket is opened with an HTTP request to the same address.
        snap.websocket = snap.websocket_url.as_deref().and_then(|u| match u.strip_prefix("wss://") {
            Some(rest) if tls_for_websockets() => self.at(&format!("https://{rest}")),
            Some(_) => None,
            None => self.at(&u.replacen("ws://", "http://", 1)),
        });
        // Behind TLS (`https://`), HTTP/2 is agreed in the handshake; `at` knows whether this side speaks TLS.
        snap.grpc = snap.grpc_url.as_deref().and_then(|u| self.at(u));
        for route in snap.dynamic_routes.iter_mut() {
            // A route with a backend this side cannot reach is kept, without one: what it matches is Python's.
            route.backend = route.backend_url.as_deref().filter(|_| route.timeout().is_some()).and_then(|u| self.of_route(u, route.never_sent));
        }
        snap.agents = snap.agent_urls.iter().filter_map(|(slug, url)| Some((slug.clone(), self.at(url)?))).collect();
    }
}

pub struct State {
    pub snapshot: arc_swap::ArcSwap<Snapshot>,
    /// Counts snapshot replacements, so a worker can tell cheaply that the one it holds is current.
    pub generation: AtomicU64,
    /// Credentials the Python gateway has just refused.
    pub denied: Mutex<Denied>,
    /// Where plain HTTP requests go.
    pub backend: Arc<Backend>,
    pub backends: Backends,
    pub control: Upstream,
    /// Present when the snapshot comes from the control plane; native serving needs it fresh.
    pub sync: Option<Arc<Link>>,
    /// Present with a control-plane link: what was served is reported back.
    pub reporter: Option<Arc<Reporter>>,
    /// Present when a line per native request was asked for.
    pub access_log: Option<AccessLog>,
    /// This side's connection to the MQTT broker.
    pub mqtt: Arc<crate::mqtt::Link>,
    /// What this side presents to the Python gateway to ask it to shape an answer.
    pub token: Option<HeaderValue>,
}

impl State {
    pub fn replace(&self, mut snap: Snapshot) {
        self.backends.resolve(&mut snap);
        self.snapshot.store(Arc::new(snap));
        self.generation.fetch_add(1, Ordering::Release);
    }
}

/// One core's share of the data plane: its own connections to the upstreams, its own counters,
/// and its own handle on the snapshot, so that serving a request touches nothing another core
/// is writing to.
pub struct Worker {
    pub shared: Arc<State>,
    client: Client<HttpConnector, Body>,
    /// Connections to backends behind TLS, when this side speaks it.
    secure: Option<Client<hyper_rustls::HttpsConnector<HttpConnector>, Body>>,
    /// Connections to gRPC backends, which are spoken to in HTTP/2 from the first byte.
    grpc: Client<HttpConnector, Body>,
    /// The same for gRPC backends behind TLS, when this side speaks TLS to backends.
    grpc_secure: Option<Client<hyper_rustls::HttpsConnector<HttpConnector>, Body>>,
    /// The snapshot as of `generation`, behind a reference count only this worker touches.
    held: Mutex<(u64, Arc<Arc<Snapshot>>)>,
    index: usize,
}

/// How long a credential the Python gateway refused is refused here without asking again.
const DENIED_FOR: Duration = Duration::from_secs(10);
/// The most refused credentials remembered. Past it, they are all forgotten and asked about again.
const DENIED_MAX: usize = 65_536;

/// Credentials (as digests) the Python gateway answered with its "invalid key" 401, each with the
/// generation it was learned under and when. An entry counts only while that generation is still
/// the current one, so any change to the snapshot (a key re-enabled, an expiry moved) and any
/// break in the link to the control plane makes every worker ask again. A key that does not exist
/// yet cannot be in here: nobody can present a key before the gateway has made it up.
///
/// Shared by all workers, so one answer from the Python gateway serves them all. Only requests
/// with a credential this side does not know come here; requests with a working key never do.
#[derive(Default)]
pub struct Denied(HashMap<Digest32, (u64, Instant)>);

impl Denied {
    fn holds(&self, digest: &Digest32, generation: u64, now: Instant) -> bool {
        matches!(self.0.get(digest), Some((g, at)) if *g == generation && now.duration_since(*at) < DENIED_FOR)
    }

    fn learn(&mut self, digest: Digest32, generation: u64, now: Instant) {
        if self.0.len() >= DENIED_MAX {
            self.0.clear();
        }
        self.0.insert(digest, (generation, now));
    }
}

impl Worker {
    pub fn new(shared: Arc<State>, index: usize) -> Worker {
        let mut connector = HttpConnector::new();
        connector.set_nodelay(true);
        let client = Client::builder(TokioExecutor::new())
            .pool_idle_timeout(Duration::from_secs(30))
            .pool_max_idle_per_host(512)
            .build(connector);
        let mut connector = HttpConnector::new();
        connector.set_nodelay(true);
        connector.set_connect_timeout(Some(GRPC_CONNECT_TIMEOUT));
        let grpc = Client::builder(TokioExecutor::new()).http2_only(true).pool_idle_timeout(Duration::from_secs(90)).build(connector);
        let held = Mutex::new((shared.generation.load(Ordering::Acquire), Arc::new(shared.snapshot.load_full())));
        let secure = tls().map(|config| {
            let mut connector = HttpConnector::new();
            connector.set_nodelay(true);
            connector.enforce_http(false);
            let connector =
                hyper_rustls::HttpsConnectorBuilder::new().with_tls_config((**config).clone()).https_only().enable_http1().wrap_connector(connector);
            Client::builder(TokioExecutor::new()).pool_idle_timeout(Duration::from_secs(30)).pool_max_idle_per_host(512).build(connector)
        });
        let grpc_secure = tls().map(|config| {
            let mut connector = HttpConnector::new();
            connector.set_nodelay(true);
            connector.set_connect_timeout(Some(GRPC_CONNECT_TIMEOUT));
            connector.enforce_http(false);
            let connector =
                hyper_rustls::HttpsConnectorBuilder::new().with_tls_config((**config).clone()).https_only().enable_http2().wrap_connector(connector);
            Client::builder(TokioExecutor::new()).http2_only(true).pool_idle_timeout(Duration::from_secs(90)).build(connector)
        });
        Worker { shared, client, secure, grpc, grpc_secure, held, index }
    }

    /// Send a request to an upstream, over TLS if that is how it is reached.
    fn send(&self, to: &Upstream, req: Request<Body>) -> hyper_util::client::legacy::ResponseFuture {
        match &self.secure {
            Some(secure) if to.tls => secure.request(req),
            _ => self.client.request(req),
        }
    }

    /// The current snapshot and the generation it belongs to.
    fn snapshot(&self) -> (u64, Arc<Arc<Snapshot>>) {
        let generation = self.shared.generation.load(Ordering::Acquire);
        let mut held = self.held.lock().unwrap();
        if held.0 != generation {
            *held = (generation, Arc::new(self.shared.snapshot.load_full()));
        }
        (generation, held.1.clone())
    }
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

/// The MQTT paths with handlers of their own in the Python gateway that this side answers too.
/// Every other path under `/mqtt` is the Python gateway's.
const PUBLISH: &str = "/mqtt/publish";
const SUBSCRIBE: &str = "/mqtt/subscribe";
/// The longest body to publish from that is read here; a longer one is the Python gateway's.
const PUBLISH_BODY_MAX: u64 = 1 << 20;

/// First path segments after `/api/` that the control plane keeps although its router would call
/// them plain HTTP.
const KEPT_BY_CONTROL: [&str; 2] = ["ws", "agents"];

/// The protocol a path belongs to, decided in the order the Python gateway's router decides it
/// (prefixes are compared as text, so `/mcpx` is MCP there and here). `None` for what the control
/// plane keeps: MQTT but for publishing, WebSocket, gRPC and its own endpoints. A path with no
/// prefix, or under `/rest` or `/http`, is plain HTTP when the control plane has said which paths
/// are its own (`own`) and this is none of them; until it has, only `/api/...` is.
fn classify(path: &str, own: Option<&[String]>) -> Option<Protocol> {
    let under = |name: &str| path.starts_with(name) || path.strip_prefix("/api").map(|rest| rest.starts_with(name)).unwrap_or(false);
    if path == PUBLISH || path == SUBSCRIBE {
        Some(Protocol::Mqtt)
    } else if under("/mcp") {
        Some(Protocol::Mcp)
    } else if under("/a2a") {
        Some(Protocol::A2a)
    } else if under("/agui") {
        Some(Protocol::AgUi)
    } else if under("/acp") {
        Some(Protocol::Acp)
    } else if under("/mqtt") || path.starts_with("/ws") || path.starts_with("/websocket") {
        None
    } else if under("/grpc") {
        Some(Protocol::Bridge)
    } else if under("/graphql") {
        Some(Protocol::GraphQl)
    } else if let Some(rest) = path.strip_prefix("/api/") {
        (!KEPT_BY_CONTROL.contains(&rest.split('/').next().unwrap_or(""))).then_some(Protocol::Http)
    } else {
        own.filter(|own| !own.iter().any(|p| path.starts_with(p.as_str()))).map(|_| Protocol::Http)
    }
}

/// The path an ACP or AG-UI backend is sent: the gateway's prefix (`/acp`, `/api/acp`, `/agui`,
/// `/api/agui`) is taken off, so the server sees the paths its specification names. A GraphQL
/// path loses only a leading `/api`: the server is sent `/graphql` either way.
fn upstream_path(path: &str, protocol: Protocol) -> &str {
    let segment = match protocol {
        Protocol::GraphQl => return path.strip_prefix("/api").filter(|rest| rest.starts_with("/graphql")).unwrap_or(path),
        Protocol::Acp => "/acp",
        Protocol::AgUi => "/agui",
        _ => return path,
    };
    for base in [path, path.strip_prefix("/api").unwrap_or("")] {
        match base.strip_prefix(segment) {
            Some("") => return "/",
            Some(rest) if rest.starts_with('/') => return rest,
            _ => {}
        }
    }
    path
}

/// A byte the Python gateway's HTTP client (httpx) leaves as it is in a path: unreserved, a
/// sub-delimiter, or one of `:/[]@`.
fn path_safe(b: u8) -> bool {
    b.is_ascii_alphanumeric() || b"-._~!$&'()*+,;=:/[]@".contains(&b)
}

/// Whether a path reaches a backend exactly as written: nothing to escape, no `.` or `..` segment.
fn plain_path(path: &str) -> bool {
    path.bytes().all(path_safe) && !path.contains("/.")
}

/// The bytes of a path with its percent-escapes decoded. A `%` that does not start an escape stays.
fn unescaped(raw: &str) -> Vec<u8> {
    let bytes = raw.as_bytes();
    let hex = |b: u8| (b as char).to_digit(16).map(|d| d as u8);
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        match (bytes[i], bytes.get(i + 1).copied().and_then(hex), bytes.get(i + 2).copied().and_then(hex)) {
            (b'%', Some(high), Some(low)) => {
                out.push(high << 4 | low);
                i += 3;
            }
            (b, _, _) => {
                out.push(b);
                i += 1;
            }
        }
    }
    out
}

/// The path as the Python gateway reads it: its server decodes percent-escapes before anything
/// looks at the path, so keys' paths, policies, routes and protocols are all decided on the
/// decoded text. `None` for a path the two sides might not read alike, which is the Python
/// gateway's: not ASCII as sent, not UTF-8 once decoded, or holding a control character, `?` or `#`.
fn decoded(raw: &str) -> Option<Cow<'_, str>> {
    if !raw.is_ascii() {
        return None;
    }
    if !raw.contains('%') {
        return Some(Cow::Borrowed(raw));
    }
    let text = String::from_utf8(unescaped(raw)).ok()?;
    (!text.chars().any(|c| c.is_ascii_control() || c == '?' || c == '#')).then_some(Cow::Owned(text))
}

/// The path a backend is asked for, given the whole path the Python gateway hands its HTTP
/// client (the backend's own prefix, then the request's decoded path). That client drops `.` and
/// `..` segments, then escapes what a path may not hold; a path already made only of safe
/// characters and whole escapes is left as it is, and otherwise a `%` is escaped too.
fn sent_path(full: &str) -> String {
    let mut kept: Vec<&str> = Vec::new();
    for part in full.split('/') {
        match part {
            "." => {}
            ".." => {
                if !kept.is_empty() && kept != [""] {
                    kept.pop();
                }
            }
            other => kept.push(other),
        }
    }
    let path = kept.join("/");
    if path.is_empty() {
        return "/".to_owned();
    }
    let b = path.as_bytes();
    let whole_escapes = (0..b.len()).all(|i| b[i] != b'%' || (b.get(i + 1).is_some_and(u8::is_ascii_hexdigit) && b.get(i + 2).is_some_and(u8::is_ascii_hexdigit)));
    if whole_escapes && b.iter().all(|c| path_safe(*c) || *c == b'%') {
        return path;
    }
    let mut out = String::with_capacity(path.len() + 8);
    for c in b {
        match path_safe(*c) {
            true => out.push(*c as char),
            false => out.push_str(&format!("%{c:02X}")),
        }
    }
    out
}

/// For `conformance/paths.py`: read one case per line (`{"prefix": ..., "path": ...}`, the path
/// as a client sends it) and answer each with `{"decoded", "sent"}`, or `null` for a path this
/// side leaves to the Python gateway.
pub fn judge_paths() -> Result<(), String> {
    use std::io::{BufRead, Write};

    #[derive(serde::Deserialize)]
    struct Case {
        prefix: String,
        path: String,
    }

    let out = std::io::stdout();
    let mut out = out.lock();
    for line in std::io::stdin().lock().lines() {
        let line = line.map_err(|e| e.to_string())?;
        let case: Case = serde_json::from_str(&line).map_err(|e| format!("case is not valid: {e}"))?;
        let said = match decoded(&case.path) {
            Some(path) => {
                let sent = sent_path(&format!("{}{path}", case.prefix));
                match sent.parse::<hyper::http::uri::PathAndQuery>() {
                    Ok(_) => serde_json::json!({"decoded": path, "sent": sent}),
                    Err(_) => serde_json::Value::Null,
                }
            }
            None => serde_json::Value::Null,
        };
        writeln!(out, "{said}").map_err(|e| e.to_string())?;
    }
    Ok(())
}

/// `/v<digits>[.<digits>]` followed by `/` or nothing: a path that names its API version, which
/// the Python gateway resolves differently from one that does not. The version it names, and
/// what follows it.
fn version_named(path: &str) -> Option<(&str, &str)> {
    let named = path.strip_prefix("/v")?;
    let digits = |s: &str| s.bytes().take_while(u8::is_ascii_digit).count();
    let whole = digits(named);
    if whole == 0 {
        return None;
    }
    let mut len = whole;
    if let Some(fraction) = named[whole..].strip_prefix('.') {
        let n = digits(fraction);
        if n > 0 {
            len += 1 + n;
        }
    }
    let (version, rest) = named.split_at(len);
    (rest.is_empty() || rest.starts_with('/')).then_some((version, rest))
}

/// What a backend asked for `path` is told of the API version, and the path it is asked for
/// instead when versions are taken off on the way (`VersionManager.resolve`, as the router uses
/// it). A path that names a version is that version's: one the gateway does not know says
/// nothing, whatever the headers hint. `resolved` is what the headers or the default give a
/// path that names none. `None` when the Python gateway decides: the version adds more than its
/// number, or the path's first segment has a digit of another script, which counts as one there.
fn told_version<'a, 'p>(snap: &'a Snapshot, path: &'p str, resolved: Option<&'a str>) -> Option<(Option<&'a str>, Option<&'p str>)> {
    if let Some(named) = path.strip_prefix("/v") {
        if !named.split('/').next().unwrap_or("").is_ascii() {
            return None;
        }
    }
    let Some((version, rest)) = version_named(path) else { return Some((resolved, None)) };
    match snap.api_versions.native_ok.get_key_value(version) {
        None => Some((None, None)),
        Some((_, false)) => None,
        Some((known, true)) => {
            let stripped = snap.api_versions.strip_prefix.then_some(if rest.is_empty() { "/" } else { rest });
            Some((Some(known.as_str()), stripped))
        }
    }
}

/// The seconds of a countdown, as they stand in a page from the snapshot.
const PAGE_RETRY_AFTER: &str = "987654321";

/// The bytes of a body made here, which is whole and ready. `None` for one streamed from upstream.
fn made_here(body: Body) -> Option<Bytes> {
    let Either::Right(Either::Left(body)) = body else { return None };
    let mut body = std::pin::pin!(body);
    let mut cx = std::task::Context::from_waker(std::task::Waker::noop());
    match body.as_mut().poll_frame(&mut cx) {
        std::task::Poll::Ready(Some(Ok(frame))) => frame.into_data().ok(),
        _ => Some(Bytes::new()),
    }
}

/// Turn an error made here into the page a browser is shown, as the Python gateway renders it:
/// the page for this status and shape, with the error's code, message, hint, request id and
/// retry time written in. The status and the other headers stay. If there is no such page the
/// answer stays as it is.
fn as_page(pages: &HashMap<String, String>, resp: Response<Body>) -> Response<Body> {
    let (mut parts, body) = resp.into_parts();
    let Some(bytes) = made_here(body) else { return Response::from_parts(parts, full(Bytes::new())) };
    let error: serde_json::Value = serde_json::from_slice(&bytes).unwrap_or_default();
    let said = |name: &str| error.get(name).and_then(|v| v.as_str()).unwrap_or("");
    let details = error.get("details");
    let hint = details.and_then(|d| d.get("hint")).and_then(|h| h.as_str()).filter(|h| !h.is_empty());
    let wait = details.and_then(|d| d.get("retry_after")).and_then(|r| r.as_i64()).filter(|r| *r > 0 && parts.status == StatusCode::TOO_MANY_REQUESTS);
    let shape = format!("{}:{}{}", parts.status.as_u16(), u8::from(hint.is_some()), u8::from(wait.is_some()));
    let Some(page) = pages.get(&shape) else { return Response::from_parts(parts, full(bytes)) };
    // The caller's own words (a request id it chose) go in last, so nothing in them is read as a marker.
    let mut page = match wait {
        Some(seconds) => page.replace(PAGE_RETRY_AFTER, &seconds.to_string()),
        None => page.clone(),
    };
    page = page.replace("@@code@@", &html_escaped(said("error")));
    if let Some(hint) = hint {
        page = page.replace("@@hint@@", &html_escaped(hint));
    }
    page = page.replace("@@message@@", &html_escaped(said("message")));
    page = page.replace("@@request-id@@", &html_escaped(said("request_id")));
    parts.headers.insert(CONTENT_TYPE, HeaderValue::from_static("text/html; charset=utf-8"));
    Response::from_parts(parts, full(page))
}

fn full(bytes: impl Into<Bytes>) -> Body {
    Either::Right(Either::Left(Full::new(bytes.into())))
}

fn json(text: &str) -> String {
    serde_json::to_string(text).unwrap_or_else(|_| "\"\"".into())
}

/// A time since the epoch as UTC text: `YYYY-MM-DDTHH:MM:SS.ffffff`, the shape the Python gateway
/// puts in error bodies (it leaves the fraction out when it is zero), or with `log`,
/// `YYYY-MM-DDTHH:MM:SS.mmmZ`, the shape of its log lines.
pub fn timestamp(d: Duration, log: bool) -> String {
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
    let (h, m, s) = (rem / 3600, (rem % 3600) / 60, rem % 60);
    if log {
        format!("{year:04}-{month:02}-{day:02}T{h:02}:{m:02}:{s:02}.{:03}Z", micros / 1000)
    } else if micros == 0 {
        format!("{year:04}-{month:02}-{day:02}T{h:02}:{m:02}:{s:02}")
    } else {
        format!("{year:04}-{month:02}-{day:02}T{h:02}:{m:02}:{s:02}.{micros:06}")
    }
}

fn wall() -> Duration {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default()
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
            (t.len() == 32 && t.bytes().all(|c| c.is_ascii_hexdigit())).then_some(t)
        });
    let traceparent = match trace_id {
        Some(t) => format!("00-{t}-{:016x}-01", fastrand::u64(..)),
        None => format!("00-{:016x}{:016x}-{:016x}-01", fastrand::u64(..), fastrand::u64(..), fastrand::u64(..)),
    };
    Trace { request_id, traceparent }
}

/// A request the data plane serves itself.
struct Native<'a> {
    protocol: Protocol,
    /// The protocol's backend, when it is not the plain HTTP one.
    backend: Option<&'a Backend>,
    entry: &'a KeyEntry,
    /// The dynamic route the path matches, which then decides where the request goes and how.
    route: Option<&'a DynamicRoute>,
    version: Option<&'a str>,
    /// The version the backend is told, which is the path's own when it names one.
    told: Option<&'a str>,
    /// The client's address, as the Python gateway would resolve it.
    ip: IpAddr,
    method: &'static str,
    /// The caller is a browser: an error made here is shown as a page.
    html: bool,
    /// What the transformation rules that apply do to the request and to its answer.
    shaping: Shaping<'a>,
    /// The path the backend is asked for, when a rule rewrites it or its version is taken off.
    rewritten: Option<String>,
}

/// A request refused here with a 401: it carries no credential that works.
struct Unauthenticated<'a> {
    /// For a browser, the pages errors are shown as.
    pages: Option<&'a HashMap<String, String>>,
    wording: &'a Wording,
    version: Option<&'a str>,
    ip: IpAddr,
    method: &'static str,
}

/// The answer to a preflight, as the Python gateway's CORS middleware words it.
struct Preflighted<'a> {
    allowed: bool,
    headers: Vec<(String, String)>,
    body: String,
    version: Option<&'a str>,
    ip: IpAddr,
}

/// An `OPTIONS` request that is not a preflight, which the Python gateway's proxy handler
/// answers before anything else is looked at: 200, nothing in the body, and three headers that
/// say any origin may ask anything.
struct Unasked<'a> {
    version: Option<&'a str>,
    ip: IpAddr,
}

/// The headers of the answer to an `OPTIONS` request that is not a preflight.
const OPTIONS_ANSWER: [(&str, &str); 3] = [
    ("access-control-allow-origin", "*"),
    ("access-control-allow-methods", "GET, POST, PUT, DELETE, PATCH, OPTIONS, HEAD"),
    ("access-control-allow-headers", "*"),
];

/// Answer a preflight (an `OPTIONS` request with `Origin` and `Access-Control-Request-Method`)
/// as Starlette's `CORSMiddleware.preflight_response` does: whether it is allowed, the headers
/// and the text. `None` for anything else, or for one this side cannot be sure of reading the
/// same way (a header sent twice, or not plain text).
fn preflight(rules: &crate::snapshot::CrossOrigin, headers: &HeaderMap) -> Option<(bool, Vec<(String, String)>, String)> {
    let how = rules.preflight.as_ref()?;
    let once = |name: &str| {
        let mut all = headers.get_all(name).iter();
        match (all.next(), all.next()) {
            (None, _) => Some(None),
            (Some(v), None) => v.to_str().ok().filter(|s| s.is_ascii()).map(Some),
            _ => None,
        }
    };
    let origin = once("origin")??;
    let method = once("access-control-request-method")??;
    let asked = once("access-control-request-headers")?;
    let mut answer = how.headers.clone();
    let mut set = |name: &str, value: &str| match answer.iter_mut().find(|(n, _)| n == name) {
        Some(found) => found.1 = value.to_owned(),
        None => answer.push((name.to_owned(), value.to_owned())),
    };
    let mut failures = Vec::new();
    if rules.any_origin || rules.origins.contains(origin) {
        if how.explicit_origin {
            set("Access-Control-Allow-Origin", origin);
        }
    } else {
        failures.push("origin");
    }
    if !how.methods.iter().any(|m| m == method) {
        failures.push("method");
    }
    match asked {
        Some(asked) if how.all_headers => set("Access-Control-Allow-Headers", asked),
        Some(asked) => {
            let lower = asked.to_ascii_lowercase();
            if lower.split(',').any(|h| !how.allow_headers.iter().any(|a| a == h.trim_matches([' ', '\t']))) {
                failures.push("headers");
            }
        }
        None => {}
    }
    let body = if failures.is_empty() { "OK".to_owned() } else { format!("Disallowed CORS {}", failures.join(", ")) };
    Some((failures.is_empty(), answer, body))
}

/// For `conformance/preflight.py`: read one case per line (`{"rules": ..., "headers": [[name,
/// value], ...]}`) and answer each with `{"status", "headers", "body"}`, or `null` for a request
/// this side leaves to the Python gateway.
pub fn judge_preflights() -> Result<(), String> {
    use std::io::{BufRead, Write};

    #[derive(serde::Deserialize)]
    struct Case {
        rules: crate::snapshot::CrossOrigin,
        headers: Vec<(String, String)>,
    }

    let out = std::io::stdout();
    let mut out = out.lock();
    for line in std::io::stdin().lock().lines() {
        let line = line.map_err(|e| e.to_string())?;
        let case: Case = serde_json::from_str(&line).map_err(|e| format!("case is not valid: {e}"))?;
        let mut sent = HeaderMap::new();
        for (name, value) in &case.headers {
            let name = HeaderName::from_bytes(name.as_bytes()).map_err(|e| e.to_string())?;
            sent.append(name, HeaderValue::from_bytes(value.as_bytes()).map_err(|e| e.to_string())?);
        }
        let said = match preflight(&case.rules, &sent) {
            Some((allowed, headers, body)) => serde_json::json!({"status": if allowed { 200 } else { 400 }, "headers": headers, "body": body}),
            None => serde_json::Value::Null,
        };
        writeln!(out, "{said}").map_err(|e| e.to_string())?;
    }
    Ok(())
}

/// What to do with a request.
enum Decision<'a> {
    Native(Native<'a>),
    /// A request for an agent behind the agent edge, forwarded here without a gateway key.
    Agent(AgentCall<'a>),
    Refuse(Unauthenticated<'a>),
    /// A preflight from a web page in another origin, answered here.
    Preflight(Preflighted<'a>),
    Options(Unasked<'a>),
    /// The Python gateway answers, which is always right: it is the answer whenever this side
    /// cannot be sure of deciding the same. With a digest: the request carries one credential this
    /// side does not know, and if the Python gateway refuses it, that is worth remembering.
    Ask(Option<Digest32>),
}

/// How long past its expiry a key must be before it is refused here; nearer than that, the two
/// clocks might disagree, and the Python gateway decides.
const EXPIRY_MARGIN_SECS: i64 = 2;

/// An `Authorization` value as the Python gateway turns it into a key: every `Bearer ` taken out.
fn without_bearer(value: &[u8]) -> Cow<'_, [u8]> {
    const WORD: &[u8] = b"Bearer ";
    if !value.windows(WORD.len()).any(|w| w == WORD) {
        return Cow::Borrowed(value);
    }
    let (mut out, mut i) = (Vec::with_capacity(value.len()), 0);
    while i < value.len() {
        if value[i..].starts_with(WORD) {
            i += WORD.len();
        } else {
            out.push(value[i]);
            i += 1;
        }
    }
    Cow::Owned(out)
}

/// The token of an `Authorization: Bearer <token>` header, as the Python gateway reads one when
/// looking for an OAuth2 token: the word in any case, and the rest without the white space
/// around it. `None` when the header is not of that form or names no token.
fn bearer_token(value: &[u8]) -> Option<&[u8]> {
    let (word, rest) = value.split_at_checked(7)?;
    if !word.eq_ignore_ascii_case(b"bearer ") {
        return None;
    }
    // What Python's `str.strip` takes off an ASCII string.
    let blank = |b: &u8| matches!(b, b' ' | b'\t'..=b'\r' | 0x1c..=0x1f);
    let start = rest.iter().position(|b| !blank(b))?;
    let end = rest.iter().rposition(|b| !blank(b))?;
    Some(&rest[start..=end])
}

/// Decide what happens to a request. `denied` says whether the Python gateway has just refused
/// the credential with this digest.
fn decide<'a, B>(snap: &'a Snapshot, peer: SocketAddr, req: &Request<B>, now: u64, denied: impl Fn(&Digest32) -> bool) -> Decision<'a> {
    const PYTHON: Decision<'static> = Decision::Ask(None);
    let uri = req.uri();
    // An escaped path reads differently once decoded, and what the key may reach, what a policy
    // matches and where the request goes are all decided on the decoded one.
    let Some(path) = decoded(uri.path()).filter(|_| uri.authority().is_none()) else { return PYTHON };
    let path = &*path;
    if let Some(rest) = path.strip_prefix("/agents/") {
        return agent_call(snap, peer, req, rest).map(Decision::Agent).unwrap_or(PYTHON);
    }
    let Some(protocol) = classify(path, snap.own_paths.as_deref()) else { return PYTHON };
    if snap.fallthrough_prefixes.iter().any(|p| path.starts_with(p.as_str())) {
        return PYTHON;
    }
    // A browser is shown errors as pages, which come from the Python gateway with the snapshot.
    let html = match prefers_html(req.headers()) {
        Some(false) => false,
        Some(true) if snap.error_pages.is_some() => true,
        _ => return PYTHON,
    };
    // A request to change protocol (a WebSocket handshake) is not an ordinary request there, and
    // one from a web page in another origin is answered with the gateway's cross-origin headers.
    if req.headers().contains_key(UPGRADE) || cross_origin(snap, req.headers()).is_err() {
        return PYTHON;
    }
    let m = req.method();
    let method = if m == Method::GET {
        "GET"
    } else if m == Method::POST {
        "POST"
    } else if m == Method::PUT {
        "PUT"
    } else if m == Method::DELETE {
        "DELETE"
    } else if m == Method::PATCH {
        "PATCH"
    } else if m == Method::HEAD {
        "HEAD"
    } else if m == Method::OPTIONS {
        "OPTIONS"
    } else {
        return PYTHON;
    };
    // The version is echoed on every answer, refusals included, with what it announces if it is
    // on its way out. One that is gone, or has a backend of its own, is the Python gateway's.
    let version = match resolve_version(snap, req.headers()) {
        Some((_, false)) => return PYTHON,
        found => found.map(|(v, _)| v),
    };
    let Some(ip) = govern::client_ip(peer, req.headers(), &snap.trusted_proxies) else { return PYTHON };
    // An `OPTIONS` request carries no credential and is answered before any is looked for. A
    // preflight is one from another origin that names the method it asks about; the Python
    // gateway's CORS middleware answers it. Any other reaches its proxy handler, which answers
    // every one alike.
    if method == "OPTIONS" {
        let headers = req.headers();
        if headers.contains_key(ORIGIN) && headers.contains_key("access-control-request-method") {
            return match snap.cross_origin.as_ref().and_then(|rules| preflight(rules, headers)) {
                Some((allowed, headers, body)) => Decision::Preflight(Preflighted { allowed, headers, body, version, ip }),
                None => PYTHON,
            };
        }
        // What a page in another origin is told goes onto the answer; where this side cannot
        // say (an `Origin` given twice), the Python gateway answers.
        return if cross_origin(snap, headers).is_ok() { Decision::Options(Unasked { version, ip }) } else { PYTHON };
    }
    // Publishing is a POST to a broker the Python gateway is connected to; anything else on that
    // path is its to answer.
    let publishing = protocol == Protocol::Mqtt;
    if publishing && (method != "POST" || snap.mqtt.is_none()) {
        return PYTHON;
    }
    // The bridge takes JSON by POST, and is served here only as the Python gateway said it serves it.
    let bridging = protocol == Protocol::Bridge;
    if bridging && (method != "POST" || snap.bridge.is_none()) {
        return PYTHON;
    }
    let pages = snap.error_pages.as_ref().filter(|_| html);
    let refuse = |wording: &'a Wording| Decision::Refuse(Unauthenticated { pages, wording, version, ip, method });

    // The key: the gateway's own header first, `Authorization` when that one is absent or empty.
    let header = |name: &HeaderName| req.headers().get(name).map(|v| v.as_bytes()).filter(|v| !v.is_empty());
    let (own, authorization) = (header(&snap.key_header), header(&AUTHORIZATION));
    let (credential, source) = match (own, authorization) {
        (None, None) => return snap.refusals.as_ref().map(|r| refuse(&r.missing_key)).unwrap_or(PYTHON),
        (Some(key), _) => (Cow::Borrowed(key), b'K'),
        (None, Some(value)) => (without_bearer(value), b'A'),
    };
    // Beyond ASCII the two sides would not hash the same bytes.
    if credential.is_empty() || !credential.is_ascii() {
        return PYTHON;
    }
    // With a key in its own header that does not work, an `Authorization` header may still let
    // the request in (an OAuth2 token): only the Python gateway can tell.
    let only_credential = own.is_none() || authorization.is_none();
    let invalid = snap.refusals.as_ref().filter(|_| only_credential).map(|r| &r.invalid_key);
    // What is not a key may be an OAuth2 token, which the Python gateway looks for in
    // `Authorization` once the key has failed. With a key header as well, which of the two lets
    // the request in is its to say. A token's caller goes through the IP rules and policies
    // like a key's, further down. One that has run out is treated as unknown: the Python
    // gateway refuses it first.
    let token = || match (own, authorization) {
        (None, Some(value)) => bearer_token(value)
            .and_then(|token| snap.tokens.get(&hash_key(token)))
            .filter(|t| t.expires_at.is_some_and(|e| (now as i64) < e)),
        _ => None,
    };
    let Some(entry) = snap.keys.get(&hash_key(&credential)).or_else(token) else {
        let Some(wording) = invalid else { return PYTHON };
        // Not a key this side knows. That alone proves nothing: it may have been created a
        // moment ago. It is refused here only once the Python gateway has refused it.
        let mut text = Vec::with_capacity(credential.len() + 1);
        text.push(source);
        text.extend_from_slice(own.or(authorization).unwrap_or_default());
        let digest = hash_key(&text);
        return if denied(&digest) { refuse(wording) } else { Decision::Ask(Some(digest)) };
    };
    if !entry.is_active || !entry.native_ok {
        return PYTHON;
    }
    match entry.expires_at {
        Some(e) if e + EXPIRY_MARGIN_SECS < now as i64 => return invalid.map(refuse).unwrap_or(PYTHON),
        Some(e) if e <= now as i64 => return PYTHON,
        _ => {}
    }
    // A dynamic route comes before the protocol's own backend: the first that matches decides.
    // Publishing has a handler of its own in the Python gateway, which no route comes before.
    let route = snap.dynamic_routes.iter().find(|r| !publishing && !bridging && r.matches(path));
    let backend = match (route, protocol.route_index()) {
        (Some(route), _) => match route.backend.as_deref() {
            Some(backend) => Some(backend),
            None => return PYTHON,
        },
        // A protocol without a backend this side can reach is the Python gateway's to answer.
        (None, Some(i)) => match snap.routes[i].as_deref() {
            Some(backend) => Some(backend),
            None => return PYTHON,
        },
        (None, None) => None,
    };
    // A routed request keeps its path, or takes the route's; the protocol's prefix is not taken off.
    let asked = match route {
        Some(route) => route.rewrite.as_deref().unwrap_or(path),
        None => upstream_path(path, protocol),
    };
    // The rules that apply are applied here when this side was told all they do, and when the
    // request is one they do the same to on both sides.
    let Some(mut shaping) = snap.shaping(method, asked, path) else { return PYTHON };
    if !shaping.is_empty() {
        // Publishing and gRPC have handlers of their own in the Python gateway.
        if publishing || bridging || protocol == Protocol::Grpc {
            return PYTHON;
        }
        // An answer the Python gateway streams is passed on there with no rule applied.
        if !shaping.response.is_empty() {
            match streamed(path, method, req.headers(), uri.query()) {
                Some(false) => {}
                Some(true) => shaping.response.clear(),
                None => return PYTHON,
            }
        }
    }
    // A query is taken apart and written again there, and is here: one this side is not certain
    // to read the same is the Python gateway's. (Publishing, the bridge and gRPC send none on.)
    // So is a request that gives twice a header the Python gateway sends on one line of.
    if !(publishing || bridging || protocol == Protocol::Grpc)
        && (framed_twice(req.headers()) || uri.query().is_some_and(|query| !query_survives(query) && read_query(query).is_none()))
    {
        return PYTHON;
    }
    // A path rules rewrite is still a path.
    let Ok(mut rewritten) = shaping.rewritten(asked) else { return PYTHON };
    if rewritten.as_deref().is_some_and(|to| !to.starts_with('/')) {
        return PYTHON;
    }
    // The version is resolved from the path the request ends up with.
    let Some((told, stripped)) = told_version(snap, rewritten.as_deref().unwrap_or(asked), version) else { return PYTHON };
    if let Some(stripped) = stripped.map(str::to_owned) {
        rewritten = Some(stripped);
    }
    let asked = rewritten.as_deref().unwrap_or(asked);
    // What the backend is asked for must be something this side's HTTP client will send.
    if !plain_path(asked) && sent_path(asked).parse::<hyper::http::uri::PathAndQuery>().is_err() {
        return PYTHON;
    }
    Decision::Native(Native { protocol, backend, entry, route, version, told, ip, method, html, shaping, rewritten })
}

/// Whether the Python gateway passes the answer to a request on as a stream
/// (`should_proxy_streaming_http`): an A2A streaming message, an AG-UI request that asks for
/// events (in `Accept` or with `?sse=`), an ACP run that does (a POST that accepts them, or
/// `?stream=`). `None` when the query is not one this side is sure to read as it is read there.
fn streamed(path: &str, method: &str, headers: &HeaderMap, query: Option<&str>) -> Option<bool> {
    if path.contains("message:stream") {
        return Some(true);
    }
    let (agui, acp) = (path.contains("/agui"), path.contains("/acp"));
    if !agui && !acp {
        return Some(false);
    }
    let events = headers.get(ACCEPT).is_some_and(|v| {
        let accept = v.as_bytes().to_ascii_lowercase();
        accept.windows(17).any(|w| w == b"text/event-stream")
    });
    // An escaped query is read decoded there, and of a name given twice the last value counts
    // and is the only one sent on: both are the Python gateway's to read.
    let query = query.unwrap_or("");
    let asks = |name: &str| {
        if query.contains(['%', '+']) {
            return None;
        }
        let mut given = query.split('&').map(|pair| pair.split_once('=').unwrap_or((pair, ""))).filter(|(key, _)| *key == name);
        match (given.next(), given.next()) {
            (found, None) => Some(found.is_some_and(|(_, value)| matches!(value, "1" | "true" | "yes"))),
            _ => None,
        }
    };
    if agui && (events || asks("sse")?) {
        return Some(true);
    }
    if acp && path.contains("/runs") && ((method == "POST" && events) || asks("stream")?) {
        return Some(true);
    }
    Some(false)
}

/// Apply rules' header changes to a request's or an answer's headers. The Python gateway holds
/// them as one line per name while it does; a name the rules mention that an answer gives more
/// than once is one line there, its values joined, and is here too.
///
/// A request comes here with one line to a name, the first, as the Python gateway's rules read
/// it; `repeated` is every line of the names it gave more than once (`take_repeated`). They are
/// put back at the end, in place of the one line, where that line is still as it came: a header
/// a rule set, renamed or removed, or the gateway wrote itself, keeps what that left.
///
/// With `sent` (a request on its way to a backend), the headers the gateway itself writes are
/// written here too, where each line's exact name is still known: the Python gateway writes them
/// into the same dictionary the rules have just changed, under names of one exact spelling, so a
/// line a rule left under that very spelling is taken over and one under any other stays beside
/// it. Says whether that was done.
fn shape_headers(headers: &mut HeaderMap, rules: &[&Actions], sent: Option<&Sent>, repeated: &[(HeaderName, Vec<HeaderValue>)]) -> bool {
    if !rules.iter().any(|a| a.changes_headers()) {
        return false;
    }
    let mut lines: HeaderLines<HeaderValue> = Vec::with_capacity(headers.len());
    for name in headers.keys() {
        let mut values = headers.get_all(name).iter();
        let Some(first) = values.next() else { continue };
        if !rules.iter().any(|a| a.names(name.as_str())) {
            lines.push((name.as_str().to_owned(), first.clone()));
            lines.extend(values.map(|v| (name.as_str().to_owned(), v.clone())));
            continue;
        }
        let mut joined = first.as_bytes().to_vec();
        for more in values {
            joined.extend_from_slice(b", ");
            joined.extend_from_slice(more.as_bytes());
        }
        lines.push((name.as_str().to_owned(), HeaderValue::from_bytes(&joined).unwrap_or_else(|_| first.clone())));
    }
    // The request-id plugin adds its header before any rule is applied, when the caller sent none.
    if let Some(id) = sent.filter(|s| s.inject_request_id && !lines.iter().any(|(n, _)| n.eq_ignore_ascii_case("x-request-id"))) {
        if let Ok(v) = HeaderValue::from_str(&id.trace.request_id) {
            lines.push(("X-Request-ID".to_owned(), v));
        }
    }
    for actions in rules {
        change_headers(&mut lines, actions, |text| HeaderValue::from_str(text).ok());
    }
    if let Some(sent) = sent {
        if let Some(v) = sent.version.and_then(|v| HeaderValue::from_str(v).ok()) {
            put(&mut lines, "X-API-Version", v);
        }
        // What a version on its way out announces is written like the version itself.
        for (name, value) in sent.notices.iter().flat_map(|n| [("Warning", &n.warning), ("Sunset", &n.sunset)]) {
            if let Some(value) = value {
                put(&mut lines, name, value.clone());
            }
        }
        lines.retain(|(name, _)| !name.to_ascii_lowercase().starts_with("x-gateway-"));
        if !lines.iter().any(|(name, _)| name == "X-Forwarded-For") {
            lines.push(("X-Forwarded-For".to_owned(), HeaderValue::from_static("")));
        }
        if let Ok(v) = HeaderValue::from_str(sent.protocol) {
            put(&mut lines, "X-Protocol", v);
        }
        if let Ok(v) = HeaderValue::from_str(&sent.trace.traceparent) {
            put(&mut lines, "traceparent", v);
        }
    }
    for (name, values) in repeated {
        if let Some(at) = lines.iter().position(|(line, value)| line == name.as_str() && value == &values[0]) {
            lines.splice(at..=at, values.iter().map(|value| (name.as_str().to_owned(), value.clone())));
        }
    }
    headers.clear();
    for (name, value) in lines {
        if let Ok(name) = HeaderName::from_bytes(name.as_bytes()) {
            headers.append(name, value);
        }
    }
    sent.is_some()
}

/// A request whose headers already carry what the gateway itself writes (`shape_headers`).
#[derive(Clone)]
struct Stamped;

/// The headers the gateway itself writes on a request to a backend, where no rule changed its
/// headers: every name is then as the caller's server read it, in lower case and once. The
/// Python gateway writes `X-API-Version`, `X-Forwarded-For` (empty) and `X-Protocol` under those
/// spellings, which are other names to its dictionary than the caller's own `x-protocol` and the
/// like: a caller's line of such a name goes on beside the gateway's, and before it. `traceparent`
/// it writes in lower case, in place of the caller's.
fn stamp_request(headers: &mut HeaderMap, sent: &Sent) {
    let internal: Vec<HeaderName> = headers.keys().filter(|k| k.as_str().starts_with("x-gateway-")).cloned().collect();
    for name in internal {
        headers.remove(name);
    }
    if sent.inject_request_id && !headers.contains_key("x-request-id") {
        if let Ok(v) = HeaderValue::from_str(&sent.trace.request_id) {
            headers.insert("x-request-id", v);
        }
    }
    if let Some(v) = sent.version.and_then(|v| HeaderValue::from_str(v).ok()) {
        headers.append("x-api-version", v);
    }
    announce_upstream(headers, sent.notices);
    headers.append("x-forwarded-for", HeaderValue::from_static(""));
    if let Ok(v) = HeaderValue::from_str(sent.protocol) {
        headers.append("x-protocol", v);
    }
    if let Ok(v) = HeaderValue::from_str(&sent.trace.traceparent) {
        headers.insert("traceparent", v);
    }
}

/// Tell a backend what the version it is told announces (`Warning`, `Sunset`), beside whatever
/// the caller sent under the same names, as the Python gateway does.
fn announce_upstream(headers: &mut HeaderMap, notices: Option<&Notices>) {
    for (name, value) in notices.iter().flat_map(|n| [("warning", &n.warning), ("sunset", &n.sunset)]) {
        if let Some(value) = value {
            headers.append(name, value.clone());
        }
    }
}

/// The longest body this side reads to apply rules to; a longer one is the Python gateway's.
const RULE_BODY_MAX: u64 = 1 << 20;

/// What rules that change fields make of a request's body, as the Python gateway does it: a body
/// that is a JSON object is read, changed by each rule in turn and written again in Python's
/// spelling; any other is left as it came (`Ok(None)`). `Err` for a body this side is not certain
/// how Python would read or write, which is then the Python gateway's.
fn reshape(body: &[u8], rules: &[&Actions]) -> Result<Option<Vec<u8>>, ()> {
    // Not UTF-8 is not JSON to Python either, and nothing is an object that does not open as one.
    let opens = body.iter().find(|b| !matches!(b, b' ' | b'\t' | b'\n' | b'\r'));
    if opens != Some(&b'{') || std::str::from_utf8(body).is_err() {
        return Ok(None);
    }
    match pyjson::parse(body).ok_or(())? {
        mut object @ pyjson::Value::Object(_) => {
            for actions in rules {
                actions.change_body(&mut object);
            }
            let mut out = Vec::with_capacity(body.len() + 64);
            object.dump(&mut out);
            Ok(Some(out))
        }
        _ => Ok(None),
    }
}

/// What rules that change fields make of an answer's body.
enum AnswerBody {
    /// The body goes on as the backend sent it.
    AsItCame,
    /// The body the rules made of it.
    Changed(Vec<u8>),
    /// This side is not certain what the Python gateway would make of it, which then says.
    Unsure,
}

/// What rules that change fields make of an answer's body, as the Python gateway does it: a
/// body that is a JSON object is read, changed by each rule in turn and written again, unless
/// nothing is left in it, and then it goes on as it came. So does a body that is no JSON, or
/// JSON of another kind. A body that is not UTF-8 has no rule applied to it there at all, and
/// one that is a JSON string is read a second time for the object it may spell: both are the
/// Python gateway's to say, as is any object this side might read or write differently.
fn reshape_answer(body: &[u8], rules: &[&Actions]) -> AnswerBody {
    if body.is_empty() {
        return AnswerBody::AsItCame;
    }
    if std::str::from_utf8(body).is_err() {
        return AnswerBody::Unsure;
    }
    match body.iter().find(|b| !matches!(b, b' ' | b'\t' | b'\n' | b'\r')) {
        Some(b'{') => {}
        Some(b'"') => return AnswerBody::Unsure,
        _ => return AnswerBody::AsItCame,
    }
    match pyjson::parse(body) {
        Some(mut object @ pyjson::Value::Object(_)) => {
            for actions in rules {
                actions.change_body(&mut object);
            }
            if matches!(&object, pyjson::Value::Object(members) if members.is_empty()) {
                return AnswerBody::AsItCame;
            }
            let mut out = Vec::with_capacity(body.len() + 64);
            object.dump(&mut out);
            AnswerBody::Changed(out)
        }
        _ => AnswerBody::Unsure,
    }
}

/// The header an answer's own header travels under when the Python gateway is asked to shape it.
const ANSWER_HEADER: &str = "x-answer-h-";

/// Ask the Python gateway what the caller is given for an answer this side holds: its status,
/// headers and body as the rules that apply leave them. Its code for answers makes it, so the
/// answer is the one it would have given. `Err` when it cannot be asked or does not say.
async fn shaped_by_control(w: &Worker, answer: Response<Body>, method: &str, path: &str) -> Result<Response<Body>, ()> {
    let token = w.shared.token.as_ref().ok_or(())?;
    let to = &w.shared.control;
    let (parts, body) = answer.into_parts();
    let uri = Uri::builder()
        .scheme(to.scheme.clone())
        .authority(to.authority.clone())
        .path_and_query(format!("{}/internal/dataplane/shape-answer", to.prefix))
        .build()
        .map_err(|_| ())?;
    let mut ask = Request::new(body);
    *ask.method_mut() = Method::POST;
    *ask.uri_mut() = uri;
    let headers = ask.headers_mut();
    headers.insert("x-dataplane-token", token.clone());
    headers.insert("x-answer-status", HeaderValue::from(parts.status.as_u16()));
    headers.insert("x-answer-method", HeaderValue::from_str(method).map_err(|_| ())?);
    // The request's own path without its leading slash, which the rules for an answer are chosen by.
    let escaped: String = path.strip_prefix('/').unwrap_or(path).bytes().map(|b| match b.is_ascii_alphanumeric() || b"-._~/".contains(&b) {
        true => (b as char).to_string(),
        false => format!("%{b:02X}"),
    }).collect();
    headers.insert("x-answer-path", HeaderValue::from_str(&escaped).map_err(|_| ())?);
    // Each line under a name of its own, so that two lines of one name stay two on the way.
    for (i, (name, value)) in parts.headers.iter().enumerate() {
        let name = HeaderName::from_bytes(format!("{ANSWER_HEADER}{i:04}-{name}").as_bytes()).map_err(|_| ())?;
        headers.append(name, value.clone());
    }
    let said = tokio::time::timeout(Duration::from_secs(30), w.send(to, ask)).await.map_err(|_| ())?.map_err(|_| ())?;
    if said.status() != StatusCode::OK {
        return Err(());
    }
    let read = said.into_body().collect().await.map_err(|_| ())?.to_bytes();
    let end = read.iter().position(|b| *b == b'\n').ok_or(())?;

    #[derive(serde::Deserialize)]
    struct Made {
        status: u16,
        headers: Vec<(String, String)>,
    }
    let made: Made = serde_json::from_slice(&read[..end]).map_err(|_| ())?;
    let mut resp = Response::new(full(read.slice(end + 1..)));
    *resp.status_mut() = StatusCode::from_u16(made.status).map_err(|_| ())?;
    for (name, value) in made.headers {
        // A header value is bytes; it is told here with each byte as a character.
        let bytes: Vec<u8> = value.chars().map(|c| u8::try_from(u32::from(c))).collect::<Result<_, _>>().map_err(|_| ())?;
        resp.headers_mut().append(HeaderName::from_bytes(name.as_bytes()).map_err(|_| ())?, HeaderValue::from_bytes(&bytes).map_err(|_| ())?);
    }
    Ok(resp)
}

/// Give a backend's answer the shape the rules for answers give it, when one of them changes
/// fields of its body. A body known to be short and not compressed is read here; where this
/// side is certain of what the Python gateway would make of it, the rules are applied here,
/// and otherwise that gateway is asked. `Err` when the body cannot be read or it cannot be asked.
async fn answer_with_body_rules(w: &Worker, resp: Response<Body>, rules: &[&Actions], method: &str, path: &str) -> Result<Response<Body>, ()> {
    let lengths = resp.headers().get_all(CONTENT_LENGTH).iter().count();
    let short = lengths == 1 && resp.headers().get(CONTENT_LENGTH).and_then(|v| v.to_str().ok()).and_then(|v| v.parse::<u64>().ok()).is_some_and(|n| n <= RULE_BODY_MAX);
    if !short || resp.headers().contains_key(hyper::header::CONTENT_ENCODING) {
        return shaped_by_control(w, resp, method, path).await;
    }
    let (mut parts, body) = resp.into_parts();
    let bytes = body.collect().await.map_err(|_| ())?.to_bytes();
    let body = match reshape_answer(&bytes, rules) {
        AnswerBody::Unsure => return shaped_by_control(w, Response::from_parts(parts, full(bytes)), method, path).await,
        AnswerBody::AsItCame => bytes,
        AnswerBody::Changed(body) => {
            parts.headers.insert(CONTENT_LENGTH, HeaderValue::from(body.len()));
            Bytes::from(body)
        }
    };
    let mut resp = Response::from_parts(parts, full(body));
    shape_answer(&mut resp, rules);
    Ok(resp)
}

/// Apply the rules to a request before it is sent on: its headers, and its query. `rewritten`
/// says the rules made a new body for it (already in place, with its length): the Python
/// gateway's HTTP client then calls it JSON unless the headers say what it is.
fn shape_request(req: &mut Request<Body>, rules: &[&Actions], rewritten: bool, sent: Option<&Sent>) {
    let repeated = if rules.iter().any(|a| a.changes_headers()) { take_repeated(req.headers_mut()) } else { Vec::new() };
    if shape_headers(req.headers_mut(), rules, sent, &repeated) {
        req.extensions_mut().insert(Stamped);
    }
    if rewritten && !req.headers().contains_key(CONTENT_TYPE) {
        req.headers_mut().insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
    }
    // The query is read into a dictionary there, changed by the rules and written again. Written
    // again it is the same query when it is plain pairs and no rule changes it.
    let uri = req.uri();
    let changing = rules.iter().any(|a| a.changes_query());
    let as_it_came = !changing && uri.query().is_none_or(query_survives);
    if let Some(mut query) = (!as_it_came).then(|| read_query(uri.query().unwrap_or(""))).flatten() {
        change_query(&mut query, rules);
        let target = match write_query(&query) {
            Some(query) => format!("{}?{query}", uri.path()),
            None => uri.path().to_owned(),
        };
        let mut parts = std::mem::take(req.uri_mut()).into_parts();
        if let Ok(target) = target.parse() {
            parts.path_and_query = Some(target);
        }
        if let Ok(uri) = Uri::from_parts(parts) {
            *req.uri_mut() = uri;
        }
    }
}

/// Apply the rules to a backend's answer: its headers, and its status.
fn shape_answer(resp: &mut Response<Body>, rules: &[&Actions]) {
    shape_headers(resp.headers_mut(), rules, None, &[]);
    let was = resp.status().as_u16();
    let becomes = change_status(was, rules);
    if becomes != was {
        if let Ok(status) = StatusCode::from_u16(becomes) {
            *resp.status_mut() = status;
        }
    }
}

/// `dataplane --judge-shapes`: each line on stdin is `{"request": [...], "response": [...],
/// "headers": [[name, value], ...], "query": "...", "answer": [[name, value], ...], "status": n}`,
/// the rules' actions as the snapshot carries them and an exchange to apply them to. The answer
/// is a line with what the backend is then sent and what the caller is: `{"headers", "query",
/// "answer", "status"}`. The conformance suite compares it with the Python gateway's own code.
pub fn judge_shapes() -> Result<(), String> {
    use std::io::{BufRead, Write};

    #[derive(serde::Deserialize)]
    struct Case {
        request: Vec<Actions>,
        response: Vec<Actions>,
        headers: Vec<(String, String)>,
        query: String,
        /// The request's body, in hex, when it has one.
        body: Option<String>,
        /// The path the backend would be asked for.
        #[serde(default)]
        path: Option<String>,
        answer: Vec<(String, String)>,
        /// The answer's body, in hex, when it has one.
        #[serde(default)]
        answer_body: Option<String>,
        status: u16,
        /// The protocol name the backend is told. With it, the headers the gateway itself writes
        /// are written too (the trace as `<trace>`), as they are on the way to a backend.
        #[serde(default)]
        protocol: Option<String>,
        /// The version the backend is told, and what it announces: `[version, warning, sunset]`.
        #[serde(default)]
        version: Option<(String, Option<String>, Option<String>)>,
    }

    fn unhex(text: &str) -> Result<Vec<u8>, String> {
        (0..text.len() / 2).map(|i| u8::from_str_radix(text.get(2 * i..2 * i + 2).unwrap_or("?"), 16).map_err(|e| e.to_string())).collect()
    }

    fn map(lines: &[(String, String)]) -> Result<HeaderMap, String> {
        let mut headers = HeaderMap::new();
        for (name, value) in lines {
            let name = HeaderName::from_bytes(name.as_bytes()).map_err(|e| e.to_string())?;
            headers.append(name, HeaderValue::from_str(value).map_err(|e| e.to_string())?);
        }
        Ok(headers)
    }

    fn lines(headers: &HeaderMap) -> Vec<(&str, &str)> {
        headers.iter().map(|(name, value)| (name.as_str(), value.to_str().unwrap_or("?"))).collect()
    }

    let out = std::io::stdout();
    let mut out = out.lock();
    for line in std::io::stdin().lock().lines() {
        let line = line.map_err(|e| e.to_string())?;
        let mut case: Case = serde_json::from_str(&line).map_err(|e| format!("case is not valid: {e}"))?;
        if case.request.iter_mut().chain(case.response.iter_mut()).any(|a| a.ready().is_none()) {
            writeln!(out, "\"unready\"").map_err(|e| e.to_string())?;
            continue;
        }
        let (before, after): (Vec<&Actions>, Vec<&Actions>) = (case.request.iter().collect(), case.response.iter().collect());
        // A query this side is not certain to read as the Python gateway does is that gateway's.
        if read_query(&case.query).is_none() {
            writeln!(out, "\"ask\"").map_err(|e| e.to_string())?;
            continue;
        }
        let target = if case.query.is_empty() { "/x".to_owned() } else { format!("/x?{}", case.query) };
        let mut req = Request::builder().uri(target).body(full(Bytes::new())).map_err(|e| e.to_string())?;
        *req.headers_mut() = map(&case.headers)?;
        let mut body = case.body.as_deref().map(unhex).transpose()?;
        let mut rewritten = false;
        if let Some(came) = body.as_ref().filter(|b| !b.is_empty() && before.iter().any(|a| a.changes_body())) {
            match reshape(came, &before) {
                Ok(None) => {}
                Ok(Some(made)) => {
                    req.headers_mut().insert(CONTENT_LENGTH, HeaderValue::from(made.len()));
                    rewritten = true;
                    body = Some(made);
                }
                Err(()) => {
                    writeln!(out, "\"ask\"").map_err(|e| e.to_string())?;
                    continue;
                }
            }
        }
        let trace = Trace { request_id: "<id>".to_owned(), traceparent: "<trace>".to_owned() };
        let read = |text: &Option<String>| text.as_deref().map(HeaderValue::from_str).transpose().map_err(|e| e.to_string());
        let notices = match &case.version {
            Some((_, warning, sunset)) => Some(Notices { warning: read(warning)?, sunset: read(sunset)?, deprecation: true }),
            None => None,
        };
        let version = case.version.as_ref().map(|(version, _, _)| version.as_str());
        let sent = case.protocol.as_deref().map(|protocol| Sent { trace: &trace, version, notices: notices.as_ref(), inject_request_id: false, protocol, path: None });
        shape_request(&mut req, &before, rewritten, sent.as_ref());
        if let Some(sent) = sent.as_ref().filter(|_| req.extensions().get::<Stamped>().is_none()) {
            stamp_request(req.headers_mut(), sent);
        }
        let mut resp = Response::new(full(Bytes::new()));
        *resp.status_mut() = StatusCode::from_u16(case.status).map_err(|e| e.to_string())?;
        *resp.headers_mut() = map(&case.answer)?;
        let mut given = case.answer_body.as_deref().map(unhex).transpose()?;
        if let Some(came) = given.as_ref().filter(|_| after.iter().any(|a| a.changes_body())) {
            match reshape_answer(came, &after) {
                AnswerBody::AsItCame => {}
                AnswerBody::Changed(made) => {
                    resp.headers_mut().insert(CONTENT_LENGTH, HeaderValue::from(made.len()));
                    given = Some(made);
                }
                AnswerBody::Unsure => {
                    writeln!(out, "\"ask python\"").map_err(|e| e.to_string())?;
                    continue;
                }
            }
        }
        shape_answer(&mut resp, &after);
        let path = match case.path.as_deref().map(|asked| Shaping { request: before.clone(), response: Vec::new() }.rewritten(asked).map(|to| to.unwrap_or_else(|| asked.to_owned()))) {
            Some(Err(())) => {
                writeln!(out, "\"ask\"").map_err(|e| e.to_string())?;
                continue;
            }
            Some(Ok(path)) => Some(path),
            None => None,
        };
        let said = serde_json::json!({
            "headers": lines(req.headers()),
            "query": req.uri().query(),
            "body": body.map(|b| b.iter().map(|byte| format!("{byte:02x}")).collect::<String>()),
            "path": path,
            "answer": lines(resp.headers()),
            "given": given.map(|b| b.iter().map(|byte| format!("{byte:02x}")).collect::<String>()),
            "status": resp.status().as_u16(),
        });
        writeln!(out, "{said}").map_err(|e| e.to_string())?;
    }
    Ok(())
}

/// What the answer to a request from another origin is given, as the Python gateway's CORS
/// middleware gives it to the answers that pass through it.
#[derive(Debug, PartialEq)]
struct Granted {
    /// `Access-Control-Allow-Origin: *`.
    any_origin: bool,
    credentials: bool,
    /// The request's own origin, named back (with `Vary: Origin`): it is on the list, or every
    /// origin is welcome and the request carries a cookie.
    named: Option<HeaderValue>,
}

/// What a request's `Origin` header earns its answer. `Ok(None)`: there is none, and nothing is
/// added. `Err`: this side cannot tell (no word from the control plane, the header twice or not
/// text), and the request is the Python gateway's.
fn cross_origin(snap: &Snapshot, headers: &HeaderMap) -> Result<Option<Granted>, ()> {
    let mut sent = headers.get_all(ORIGIN).iter();
    let Some(origin) = sent.next() else { return Ok(None) };
    let rules = snap.cross_origin.as_ref().ok_or(())?;
    let text = origin.to_str().map_err(|_| ())?;
    if sent.next().is_some() {
        return Err(());
    }
    let named = match rules.any_origin {
        true => headers.contains_key(COOKIE),
        false => rules.origins.contains(text),
    };
    Ok(Some(Granted { any_origin: rules.any_origin, credentials: rules.credentials, named: named.then(|| origin.clone()) }))
}

impl Granted {
    /// Write the headers onto an answer, over whatever the backend sent under the same names.
    fn onto(&self, resp: &mut Response<Body>) {
        let h = resp.headers_mut();
        if self.any_origin {
            h.insert(ACCESS_CONTROL_ALLOW_ORIGIN, HeaderValue::from_static("*"));
        }
        if self.credentials {
            h.insert(ACCESS_CONTROL_ALLOW_CREDENTIALS, HeaderValue::from_static("true"));
        }
        if let Some(origin) = &self.named {
            h.insert(ACCESS_CONTROL_ALLOW_ORIGIN, origin.clone());
            // After the first `Vary` the answer has; any further ones are dropped, as there.
            let vary = match h.get(VARY).map(|v| v.as_bytes()) {
                Some(first) => HeaderValue::from_bytes(&[first, b", Origin"].concat()).unwrap_or(HeaderValue::from_static("Origin")),
                None => HeaderValue::from_static("Origin"),
            };
            h.insert(VARY, vary);
        }
    }
}

/// A request for `/agents/<slug>/...` this side forwards itself.
struct AgentCall<'a> {
    backend: &'a Backend,
    /// What the agent is asked for: the path after `/agents/<slug>`, and the query.
    target: String,
    version: Option<&'a str>,
    /// The version the agent is told, which is the path's own when it names one.
    told: Option<&'a str>,
    /// What the version the agent is told announces.
    announced: Option<&'a Notices>,
    ip: IpAddr,
    method: &'static str,
    /// How a failure is answered, and whether as a page: the caller is a browser.
    failure: &'a AgentFailure,
    html: bool,
}

/// Whether the caller is shown errors as pages, as the Python gateway decides it: `text/html` is
/// accepted, JSON is not named before it, and the request is not marked as a script's. `None`
/// when the header cannot be read as text.
fn prefers_html(headers: &HeaderMap) -> Option<bool> {
    let Some(accept) = headers.get(ACCEPT) else { return Some(false) };
    let accept = accept.to_str().ok()?.to_ascii_lowercase();
    let Some(html) = accept.find("text/html") else { return Some(false) };
    if accept.find("application/json").is_some_and(|json| json < html) {
        return Some(false);
    }
    let script = headers.get("x-requested-with").is_some_and(|v| v.as_bytes().eq_ignore_ascii_case(b"xmlhttprequest"));
    Some(!script)
}

/// Text made safe inside a page, as Python's `html.escape` makes it.
fn html_escaped(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for c in text.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\'' => out.push_str("&#x27;"),
            c => out.push(c),
        }
    }
    out
}

/// Headers the Python gateway forwards to an agent but this side's HTTP client would not pass on
/// the same way, and `x-protocol`, which the Python gateway sends twice when the caller sent one.
const NOT_FORWARDED_ALIKE: [&str; 5] = ["te", "trailer", "proxy-authorization", "proxy-authenticate", "x-protocol"];

/// Whether a query reaches a backend unchanged through the Python gateway, which takes it apart
/// and writes it again (`read_query`, `write_query`): `key=value` pairs of unreserved characters,
/// no key twice.
fn query_survives(query: &str) -> bool {
    let plain = |s: &str| s.bytes().all(|b| b.is_ascii_alphanumeric() || matches!(b, b'-' | b'.' | b'_' | b'~'));
    let mut seen: Vec<&str> = Vec::new();
    query.split('&').all(|pair| match pair.split_once('=') {
        Some((key, value)) if !key.is_empty() && plain(key) && plain(value) && !seen.contains(&key) => {
            seen.push(key);
            true
        }
        _ => false,
    })
}

/// Decide a request under `/agents/`: forwarded here when the agent is one this side can reach
/// and the request would arrive there exactly as the Python gateway sends it. The agent edge asks
/// for no gateway key, applies no limit and no rule; the agent authenticates its own callers. An
/// unknown slug, a browser, and anything unusual about the request are the Python gateway's.
fn agent_call<'a, B>(snap: &'a Snapshot, peer: SocketAddr, req: &Request<B>, after: &str) -> Option<AgentCall<'a>> {
    let (uri, headers) = (req.uri(), req.headers());
    // An escaped path under the agent edge is forwarded by other code there: the Python gateway's.
    if uri.path().contains('%') {
        return None;
    }
    let (slug, rest) = after.split_once('/').unwrap_or((after, ""));
    let backend = snap.agents.get(slug)?;
    let failure = snap.agent_failure.as_ref()?;
    if snap.fallthrough_prefixes.iter().any(|p| uri.path().starts_with(p.as_str())) {
        return None;
    }
    // A browser is forwarded for like anyone else; only a failure is answered differently.
    let html = prefers_html(headers)?;
    if headers.contains_key(UPGRADE) || cross_origin(snap, headers).is_err() || NOT_FORWARDED_ALIKE.iter().any(|h| headers.contains_key(*h)) {
        return None;
    }
    // A header given twice goes on with every line, but for those sent on one line of.
    if framed_twice(headers) {
        return None;
    }
    let method = [("GET", Method::GET), ("POST", Method::POST), ("PUT", Method::PUT), ("DELETE", Method::DELETE), ("PATCH", Method::PATCH), ("HEAD", Method::HEAD)]
        .into_iter()
        .find(|(_, m)| m == req.method())?
        .0;
    let version = match resolve_version(snap, headers) {
        Some((_, false)) => return None,
        found => found.map(|(v, _)| v),
    };
    let upstream_path = format!("/{rest}");
    if snap.transformed(method, &upstream_path, uri.path()) {
        return None;
    }
    let (told, stripped) = told_version(snap, &upstream_path, version)?;
    let upstream_path = stripped.unwrap_or(&upstream_path);
    let ip = govern::client_ip(peer, headers, &snap.trusted_proxies)?;
    let prefix = &backend.upstream.prefix;
    // The query is read into a dictionary and written again, as on the way to any backend.
    let target = match uri.query() {
        Some(query) if query_survives(query) => format!("{prefix}{upstream_path}?{query}"),
        Some(query) => match write_query(&read_query(query)?) {
            Some(query) => format!("{prefix}{upstream_path}?{query}"),
            None => format!("{prefix}{upstream_path}"),
        },
        None => format!("{prefix}{upstream_path}"),
    };
    Some(AgentCall { backend, target, version, told, announced: snap.api_versions.announced(told), ip, method, failure, html })
}

/// The largest request body held here for an agent. It is held whole so that the request can
/// still be handed to the Python gateway if the agent cannot be reached; that gateway holds every
/// body whole anyway.
const AGENT_BODY_MAX: u64 = 1 << 20;

/// The Python gateway's answer when an agent's connection fails before it answers: its 500, as
/// a page for a browser and as JSON for anyone else, in its words.
fn agent_failed(failure: &AgentFailure, html: bool, t: &Trace, at: Duration) -> Response<Body> {
    let (body, kind) = if html {
        (format!("{}{}{}", failure.page.0, html_escaped(&t.request_id), failure.page.1), "text/html; charset=utf-8")
    } else {
        let body = format!(
            "{{\"error\":\"INTERNAL_ERROR\",\"message\":{},\"request_id\":{},\"timestamp\":{}}}",
            failure.message_json,
            json(&t.request_id),
            json(&timestamp(at, false)),
        );
        (body, "application/json")
    };
    let mut resp = Response::new(full(body));
    *resp.status_mut() = StatusCode::INTERNAL_SERVER_ERROR;
    resp.headers_mut().insert(CONTENT_TYPE, HeaderValue::from_static(kind));
    resp.headers_mut().insert("x-content-type-options", HeaderValue::from_static("nosniff"));
    resp
}

/// Forward a request to an agent as the Python gateway forwards it: the path after the slug, the
/// caller's headers (its `Host`, `Authorization` and `Cookie` included) and the gateway's own
/// additions. The agent's answer is streamed back as it comes. An agent that cannot be reached
/// was sent nothing, so the request goes to the Python gateway, which answers (and counts the
/// failure) in its own way.
async fn serve_agent(
    w: &Worker,
    a: &AgentCall<'_>,
    granted: Option<&Granted>,
    peer: SocketAddr,
    req: Request<Incoming>,
    started: Instant,
    at: Duration,
) -> Response<Body> {
    let has_body = matches!(a.method, "POST" | "PUT" | "PATCH" | "DELETE");
    let fits = match req.body().size_hint().exact() {
        Some(0) => true,
        Some(len) => has_body && len <= AGENT_BODY_MAX,
        None => false,
    };
    let to = &a.backend.upstream;
    let uri = Uri::builder().scheme(to.scheme.clone()).authority(to.authority.clone()).path_and_query(a.target.as_str()).build();
    let (true, Ok(uri)) = (fits, uri) else { return to_control(w, peer, req.map(Either::Left)).await };
    let (parts, body) = req.into_parts();
    let Ok(read) = body.collect().await else { return body_unreadable() };
    let bytes = read.to_bytes();
    let t = trace_for(&parts.headers);

    let mut out = Request::new(full(bytes.clone()));
    *out.method_mut() = parts.method.clone();
    *out.uri_mut() = uri;
    let mut headers = parts.headers.clone();
    for h in ["connection", "keep-alive", "transfer-encoding", "upgrade"] {
        headers.remove(h);
    }
    let internal: Vec<HeaderName> = headers.keys().filter(|k| k.as_str().starts_with("x-gateway-")).cloned().collect();
    for name in internal {
        headers.remove(name);
    }
    // Added beside whatever the caller sent under the same names, as the Python gateway adds them.
    if let Some(v) = a.told.and_then(|v| HeaderValue::from_str(v).ok()) {
        headers.append("x-api-version", v);
    }
    announce_upstream(&mut headers, a.announced);
    headers.append("x-forwarded-for", HeaderValue::from_static(""));
    headers.insert("x-protocol", HeaderValue::from_static("http"));
    if let Ok(v) = HeaderValue::from_str(&t.traceparent) {
        headers.insert("traceparent", v);
    }
    *out.headers_mut() = headers;

    let original = Request::from_parts(parts, ());
    let (mut resp, failed) = match w.send(to, out).await {
        Ok(resp) => {
            let (mut rp, rb) = resp.into_parts();
            for h in HOP_BY_HOP {
                rp.headers.remove(h);
            }
            rp.headers.remove("x-dataplane-auth");
            (Response::from_parts(rp, Either::Left(rb)), false)
        }
        Err(e) if e.is_connect() => {
            let (parts, _) = original.into_parts();
            return to_control(w, peer, Request::from_parts(parts, full(bytes))).await;
        }
        Err(_) => (agent_failed(a.failure, a.html, &t, at), true),
    };
    // The agent's answer passes through the Python gateway's CORS middleware; its own 500 does not.
    if let Some(granted) = granted.filter(|_| !failed) {
        granted.onto(&mut resp);
    }
    let answered = Answered { method: a.method, ip: a.ip, version: a.version, rejected: false, error: failed.then_some("INTERNAL_ERROR") };
    resp = finish(w, resp, &answered, &original, &t, started, at);
    if failed {
        // The Python gateway's answer to this failure carries none of its usual headers.
        for h in ["x-request-id", "x-response-time", "traceparent", "x-api-version", "warning", "sunset", "deprecation"] {
            resp.headers_mut().remove(h);
        }
    }
    resp
}

/// A WebSocket handshake this side relays itself.
struct Relay<'a> {
    backend: &'a Backend,
    /// The key that let it in. A handshake for an agent needs none.
    entry: Option<&'a KeyEntry>,
    /// What the backend is asked for: the path after `/ws` or `/agents/<slug>`, and the query.
    target: String,
}

/// Whether a request is a WebSocket handshake this side reads as the Python gateway's server
/// does: a plain `GET` for a path with nothing escaped in it, each handshake header given once.
fn is_handshake<B>(req: &Request<B>) -> bool {
    let (uri, headers) = (req.uri(), req.headers());
    let path = uri.path();
    if req.method() != Method::GET || uri.authority().is_some() || !path.is_ascii() || path.contains('%') {
        return false;
    }
    let one = |name: &str| {
        let mut all = headers.get_all(name).iter();
        all.next().filter(|_| all.next().is_none())
    };
    one("upgrade").is_some_and(|v| v.as_bytes().eq_ignore_ascii_case(b"websocket"))
        && one("sec-websocket-version").is_some_and(|v| v == "13")
        && one("sec-websocket-key").is_some()
}

/// The gateway key a handshake's query gives in `api_key`, as the Python gateway reads it: the
/// last one given, and none when that one is empty. `Err` when this side is not certain what
/// the Python gateway would read: a name or the key itself is escaped.
fn key_in_query(query: &str) -> Result<Option<&str>, ()> {
    let mut found = None;
    for part in query.split('&').filter(|p| !p.is_empty()) {
        let (name, value) = part.split_once('=').unwrap_or((part, ""));
        if name.contains('%') {
            return Err(());
        }
        if name == "api_key" {
            found = Some(value);
        }
    }
    match found {
        Some(key) if key.contains(['%', '+']) || !key.is_ascii() => Err(()),
        found => Ok(found.filter(|k| !k.is_empty())),
    }
}

/// The query a `/ws/...` backend is sent: the caller's, without the gateway's own `api_key` and
/// without empty parts, as the Python gateway writes it.
fn query_for_backend(query: &str) -> String {
    let kept: Vec<&str> = query.split('&').filter(|p| !p.is_empty() && p.split('=').next() != Some("api_key")).collect();
    kept.join("&")
}

/// Whether a request is a WebSocket handshake for `/ws/...` that is certain to be let in: the
/// key, in the query's `api_key` or else in its header, is known, working and allowed this path.
/// Anything else is the Python gateway's to judge; it relays or refuses in its own way.
fn relay<'a, B>(snap: &'a Snapshot, peer: SocketAddr, req: &Request<B>, at: Duration) -> Option<Relay<'a>> {
    let now = at.as_secs();
    let backend = snap.websocket.as_deref()?;
    let (uri, headers) = (req.uri(), req.headers());
    let path = uri.path();
    let rest = path.strip_prefix("/ws/")?;
    if !is_handshake(req) {
        return None;
    }
    let query = uri.query().unwrap_or("");
    if !query.is_ascii() {
        return None;
    }
    let key = match key_in_query(query).ok()? {
        Some(key) => key.as_bytes(),
        None => {
            let mut all = headers.get_all("x-api-key").iter();
            all.next().filter(|_| all.next().is_none()).map(|v| v.as_bytes()).filter(|k| !k.is_empty() && k.is_ascii())?
        }
    };
    let entry = snap.keys.get(&hash_key(key))?;
    let expired = entry.expires_at.is_some_and(|e| e <= now as i64);
    if !entry.is_active || !entry.native_ok || expired || entry.scope.as_ref().is_some_and(|s| !s.allows(path)) {
        return None;
    }
    // The IP rules and the policies, as for any other request with this key: a handshake is a
    // `GET` for its path. One they refuse is the Python gateway's to refuse.
    if !snap.ip_rules.is_empty() || !snap.policies.is_empty() {
        let ip = govern::client_ip(peer, headers, &snap.trusted_proxies)?;
        let tenant = entry.tenant.as_ref().map(|t| &*t.id);
        let key_id = entry.token.is_none().then_some(&*entry.id);
        if snap.ip_rules.blocking(ip, tenant, key_id).is_some() {
            return None;
        }
        let subject = Subject {
            path,
            method: "GET",
            tenant: tenant.unwrap_or(""),
            ip,
            micros_of_day: (at.as_secs() % 86_400) * 1_000_000 + u64::from(at.subsec_micros()),
        };
        if govern::refusing(&snap.policies, &subject).is_some() {
            return None;
        }
    }
    let prefix = &backend.upstream.prefix;
    let query = query_for_backend(query);
    let target = if query.is_empty() { format!("{prefix}/{rest}") } else { format!("{prefix}/{rest}?{query}") };
    Some(Relay { backend, entry: Some(entry), target })
}

/// Whether a request is a WebSocket handshake for `/agents/<slug>/...` to an agent this side can
/// reach. As for any other request to an agent, no gateway key is asked for: the agent lets its
/// own callers in. It is asked for the rest of the path and the query as they came. An unknown
/// slug is the Python gateway's to refuse.
fn agent_relay<'a, B>(snap: &'a Snapshot, req: &Request<B>) -> Option<Relay<'a>> {
    let uri = req.uri();
    let after = uri.path().strip_prefix("/agents/")?;
    if !is_handshake(req) {
        return None;
    }
    let (slug, rest) = after.split_once('/').unwrap_or((after, ""));
    let backend = snap.agents.get(slug)?;
    // Behind TLS, only where this side trusts what the Python gateway's WebSocket client trusts.
    if backend.upstream.tls && !tls_for_websockets() {
        return None;
    }
    let prefix = &backend.upstream.prefix;
    let target = match uri.query() {
        None | Some("") => format!("{prefix}/{rest}"),
        Some(query) if query.is_ascii() => format!("{prefix}/{rest}?{query}"),
        Some(_) => return None,
    };
    Some(Relay { backend, entry: None, target })
}

/// What of the caller's handshake the backend is sent, as the Python gateway's relay sends it:
/// the handshake itself, the subprotocols offered, and the caller's own credentials for the
/// backend. Never the gateway's key.
const RELAYED: [&str; 6] =
    ["sec-websocket-key", "sec-websocket-version", "sec-websocket-protocol", "sec-websocket-extensions", "authorization", "cookie"];

/// How long the backend may take to accept a WebSocket handshake.
const HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(10);

/// Open the connection to the backend and, if it accepts, join the two. If it cannot be reached
/// or refuses, the caller's handshake is refused, as the Python gateway refuses it. A backend
/// behind TLS that cannot be connected to is not refused here: the handshake comes back (`Err`)
/// for the Python gateway, which may trust a certificate this side does not.
async fn relay_websocket(w: &Worker, r: &Relay<'_>, mut req: Request<Incoming>) -> Result<Response<Body>, Request<Incoming>> {
    let refused = || {
        let mut resp = Response::new(full(Bytes::new()));
        *resp.status_mut() = StatusCode::FORBIDDEN;
        resp
    };
    let to = &r.backend.upstream;
    let Ok(uri) = Uri::builder().scheme(to.scheme.clone()).authority(to.authority.clone()).path_and_query(r.target.as_str()).build() else {
        return Ok(refused());
    };
    let mut out = Request::new(full(Bytes::new()));
    *out.uri_mut() = uri;
    for name in RELAYED {
        for value in req.headers().get_all(name) {
            out.headers_mut().append(HeaderName::from_static(name), value.clone());
        }
    }
    out.headers_mut().insert(CONNECTION, HeaderValue::from_static("upgrade"));
    out.headers_mut().insert(UPGRADE, HeaderValue::from_static("websocket"));
    match tokio::time::timeout(HANDSHAKE_TIMEOUT, w.send(to, out)).await {
        Ok(Ok(resp)) if resp.status() == StatusCode::SWITCHING_PROTOCOLS => {
            let mut resp = tunnel(hyper::upgrade::on(&mut req), resp);
            resp.headers_mut().insert("x-served-by", HeaderValue::from_static("dataplane"));
            Ok(resp)
        }
        Ok(Err(_)) if to.tls => Err(req),
        _ => Ok(refused()),
    }
}

/// How long a gRPC backend may take to accept a connection. Nothing bounds a call after that
/// but the caller's own deadline: a stream may rightly stay open for hours.
const GRPC_CONNECT_TIMEOUT: Duration = Duration::from_secs(10);

/// Whether a request is a gRPC call: HTTP/2, POST, and a gRPC content type (`application/grpc`,
/// or that with `+proto`, `+json`, ...). gRPC-Web is another protocol and is not one.
fn is_grpc<B>(req: &Request<B>) -> bool {
    req.version() == Version::HTTP_2
        && req.method() == Method::POST
        && req.headers().get(CONTENT_TYPE).is_some_and(|v| {
            v.as_bytes().strip_prefix(b"application/grpc").is_some_and(|rest| matches!(rest.first(), None | Some(b'+') | Some(b';')))
        })
}

/// A gRPC call answered here instead of by the backend: the status a gRPC client is told, its
/// message, and the HTTP status the same refusal would have had, which is what is counted and logged.
#[derive(Debug, PartialEq, Clone, Copy)]
struct GrpcRefusal {
    code: u8,
    message: &'static str,
    http: u16,
    /// The gateway's name for the error, in the request log.
    error: &'static str,
}

impl GrpcRefusal {
    const NOT_READY: Self = Self { code: 14, message: "the gateway is not ready", http: 503, error: "SERVICE_UNAVAILABLE" };
    const NOT_CONFIGURED: Self = Self { code: 12, message: "gRPC is not configured on this gateway", http: 501, error: "NOT_IMPLEMENTED" };
    const NO_KEY: Self = Self { code: 16, message: "API key required", http: 401, error: "AUTHENTICATION_ERROR" };
    const BAD_KEY: Self = Self { code: 16, message: "invalid API key", http: 401, error: "AUTHENTICATION_ERROR" };
    const NOT_APPLICABLE: Self =
        Self { code: 7, message: "the gateway's rules for this key cannot be applied to gRPC", http: 403, error: "AUTHORIZATION_ERROR" };
    const DENIED: Self = Self { code: 7, message: "access denied", http: 403, error: "AUTHORIZATION_ERROR" };
    const LIMITED: Self = Self { code: 8, message: "rate limit exceeded", http: 429, error: "RATE_LIMIT_EXCEEDED" };
    const CIRCUIT_OPEN: Self = Self { code: 14, message: "the backend is unavailable (circuit open)", http: 503, error: "SERVICE_UNAVAILABLE" };
    const NO_ANSWER: Self = Self { code: 14, message: "the backend is unavailable", http: 502, error: "BACKEND_ERROR" };

    /// The refusal for an answer the HTTP path made, by its error code.
    fn for_error(error: &str) -> Self {
        match error {
            "AUTHORIZATION_ERROR" => Self::DENIED,
            "RATE_LIMIT_EXCEEDED" => Self::LIMITED,
            "SERVICE_UNAVAILABLE" => Self::CIRCUIT_OPEN,
            _ => Self::NO_ANSWER,
        }
    }

    /// Turn an answer into the form a gRPC client reads: HTTP 200, no body, the status in the
    /// headers. What the answer was stamped with (request id, trace, `Retry-After`) stays.
    fn onto(&self, mut resp: Response<Body>) -> Response<Body> {
        *resp.status_mut() = StatusCode::OK;
        *resp.body_mut() = full(Bytes::new());
        let h = resp.headers_mut();
        h.remove(CONTENT_LENGTH);
        h.insert(CONTENT_TYPE, HeaderValue::from_static("application/grpc"));
        h.insert("grpc-status", HeaderValue::from(u16::from(self.code)));
        h.insert("grpc-message", HeaderValue::from_static(self.message));
        resp
    }
}

/// A gRPC call that goes on to the backend, and which of its headers carried the gateway's key.
struct GrpcCall<'a> {
    native: Native<'a>,
    key_in_authorization: bool,
}

/// Decide a gRPC call. There is no Python gateway to hand one to (it does not speak HTTP/2), so
/// whatever this side cannot be sure of is refused: a stale snapshot, a key it does not know (one
/// made a moment ago included), a key whose rules it cannot apply. The key's allowed paths, the IP
/// rules, the policies, the limits and the quotas are applied afterwards, as for any native request.
fn admit_grpc<'a, B>(snap: &'a Snapshot, peer: SocketAddr, req: &Request<B>, now: u64, trusted: bool) -> Result<GrpcCall<'a>, GrpcRefusal> {
    if !trusted {
        return Err(GrpcRefusal::NOT_READY);
    }
    let backend = snap.grpc.as_deref().ok_or(GrpcRefusal::NOT_CONFIGURED)?;
    let path = req.uri().path();
    // A method is `/package.Service/Method`; nothing a client library sends is escaped.
    if !path.is_ascii() || path.contains('%') {
        return Err(GrpcRefusal::DENIED);
    }
    let ip = govern::client_ip(peer, req.headers(), &snap.trusted_proxies).ok_or(GrpcRefusal::DENIED)?;
    let header = |name: &HeaderName| req.headers().get(name).map(|v| v.as_bytes()).filter(|v| !v.is_empty());
    let (credential, key_in_authorization) = match (header(&snap.key_header), header(&AUTHORIZATION)) {
        (None, None) => return Err(GrpcRefusal::NO_KEY),
        (Some(key), _) => (Cow::Borrowed(key), false),
        (None, Some(value)) => (without_bearer(value), true),
    };
    if credential.is_empty() || !credential.is_ascii() {
        return Err(GrpcRefusal::BAD_KEY);
    }
    // A key, or failing that an OAuth2 token, which arrives in `authorization` alone, as over HTTP.
    let token = || match (header(&snap.key_header), header(&AUTHORIZATION)) {
        (None, Some(value)) => bearer_token(value).and_then(|token| snap.tokens.get(&hash_key(token))),
        _ => None,
    };
    let key = snap.keys.get(&hash_key(&credential));
    let entry = key.or_else(token).ok_or(GrpcRefusal::BAD_KEY)?;
    if !entry.is_active || entry.expires_at.is_some_and(|e| e <= now as i64) {
        return Err(GrpcRefusal::BAD_KEY);
    }
    // The gateway's key is not the backend's to see; a token is the caller's own with whoever
    // issued it, and goes through as it does over HTTP.
    let key_in_authorization = key_in_authorization && key.is_some();
    // Rules this side cannot apply: to this key, or (a transformation rule, a plugin that alters
    // requests) to every request.
    if !entry.native_ok || snap.fallthrough_prefixes.iter().any(|p| path.starts_with(p.as_str())) || snap.transformed("POST", path, path) {
        return Err(GrpcRefusal::NOT_APPLICABLE);
    }
    let native = Native { protocol: Protocol::Grpc, backend: Some(backend), entry, route: None, version: None, told: None, ip, method: "POST", shaping: Shaping::default(), rewritten: None, html: false };
    Ok(GrpcCall { native, key_in_authorization })
}

/// Answer a gRPC call that never got as far as a key: counted and logged like any refusal made
/// here, under the HTTP status it stands for.
fn grpc_refused<B>(w: &Worker, refusal: &GrpcRefusal, peer: SocketAddr, req: &Request<B>, started: Instant, at: Duration) -> Response<Body> {
    let t = trace_for(req.headers());
    let mut resp = Response::new(full(Bytes::new()));
    *resp.status_mut() = StatusCode::from_u16(refusal.http).unwrap_or(StatusCode::FORBIDDEN);
    let a = Answered { method: "POST", ip: peer.ip(), version: None, rejected: true, error: Some(refusal.error) };
    refusal.onto(finish(w, resp, &a, req, &t, started, at))
}

/// Send a gRPC call to its backend over HTTP/2, body and trailers streamed both ways. The backend
/// is not sent the gateway's key; an `Authorization` that did not carry it is the caller's own
/// and goes through.
async fn forward_grpc(
    w: &Worker,
    to: &Upstream,
    peer: SocketAddr,
    mut req: Request<Body>,
    trace: &Trace,
    key_header: &HeaderName,
    key_in_authorization: bool,
) -> Result<Response<Body>, Unanswered> {
    let asked = req.uri().path_and_query().map(|p| p.as_str()).unwrap_or("/");
    let uri = Uri::builder().scheme(to.scheme.clone()).authority(to.authority.clone()).path_and_query(format!("{}{asked}", to.prefix));
    *req.uri_mut() = uri.build().map_err(|_| Unanswered::Broken)?;
    let headers = req.headers_mut();
    for h in HOP_BY_HOP {
        headers.remove(h);
    }
    headers.remove(HOST);
    headers.remove(key_header);
    if key_in_authorization {
        headers.remove(AUTHORIZATION);
    }
    // Every gRPC call says it accepts trailers: the status of the call arrives in them.
    headers.insert("te", HeaderValue::from_static("trailers"));
    let xff = match headers.get("x-forwarded-for").and_then(|v| v.to_str().ok()) {
        Some(prior) => format!("{prior}, {}", peer.ip()),
        None => peer.ip().to_string(),
    };
    if let Ok(v) = HeaderValue::from_str(&xff) {
        headers.insert("x-forwarded-for", v);
    }
    if let Ok(v) = HeaderValue::from_str(&trace.traceparent) {
        headers.insert("traceparent", v);
    }
    let sent = match &w.grpc_secure {
        Some(secure) if to.tls => secure.request(req),
        _ => w.grpc.request(req),
    };
    match sent.await {
        Ok(resp) => Ok(resp.map(Either::Left)),
        Err(e) if e.is_connect() => Err(Unanswered::Refused),
        Err(_) => Err(Unanswered::Broken),
    }
}

/// One of the gateway's own endpoints, answered here.
#[derive(Debug, PartialEq)]
enum Own<'a> {
    /// The health path: this side is serving and the Python gateway is answering. Holds the
    /// version to report, as JSON.
    Health(&'a str),
    /// `/ready`: the Python gateway's own answer, as of its latest word.
    Ready(Arc<Readiness>),
    /// `/live`: this process is running. Needs nothing from the Python gateway, so a restart of
    /// that one never makes this one look dead.
    Live,
}

/// Which of the gateway's own endpoints a request is for, if this side may answer it. `up` says
/// whether the Python gateway is known to be answering, and `ready` gives its answer to `/ready`.
/// The health path comes first, as its route does in the Python gateway.
fn own<'a, B>(snap: &'a Snapshot, req: &Request<B>, up: impl FnOnce() -> bool, ready: impl FnOnce() -> Option<Arc<Readiness>>) -> Option<Own<'a>> {
    let (uri, headers) = (req.uri(), req.headers());
    let path = uri.path();
    // The path first: almost every request is for none of these, and is done here.
    let health = snap.ops.as_ref().filter(|o| o.health_path.as_deref() == Some(path));
    if health.is_none() && path != "/ready" && path != "/live" {
        return None;
    }
    if req.method() != Method::GET || uri.authority().is_some() || headers.contains_key(UPGRADE) || cross_origin(snap, headers).is_err() {
        return None;
    }
    match (health, path) {
        (Some(ops), _) => up().then_some(Own::Health(&ops.version_json)),
        (None, "/ready") => ready().map(Own::Ready),
        (None, _) => Some(Own::Live),
    }
}

/// Answer one of the gateway's own endpoints, counted and logged as the Python gateway does its
/// own. `None` when the request is one only the Python gateway can describe.
fn answer_own<B>(w: &Worker, snap: &Snapshot, own: &Own, peer: SocketAddr, req: &Request<B>, started: Instant, at: Duration) -> Option<Response<Body>> {
    let version = match resolve_version(snap, req.headers()) {
        Some((_, false)) => return None,
        found => found.map(|(v, _)| v),
    };
    let ip = govern::client_ip(peer, req.headers(), &snap.trusted_proxies)?;
    let (status, body) = match own {
        Own::Health(version_json) => (
            StatusCode::OK,
            format!("{{\"status\":\"healthy\",\"version\":{version_json},\"timestamp\":\"{}\",\"services\":{{}}}}", timestamp(at, false)),
        ),
        Own::Ready(r) => (StatusCode::from_u16(r.status).ok()?, r.body_json.clone()),
        Own::Live => (StatusCode::OK, format!("{{\"status\":\"alive\",\"pid\":{}}}", std::process::id())),
    };
    let mut resp = Response::new(full(body));
    *resp.status_mut() = status;
    resp.headers_mut().insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
    if let Ok(Some(granted)) = cross_origin(snap, req.headers()) {
        granted.onto(&mut resp);
    }
    let a = Answered { method: "GET", ip, version, rejected: false, error: None };
    Some(finish(w, resp, &a, req, &trace_for(req.headers()), started, at))
}

/// Why a request with a valid key is refused with a 403.
enum Refusal<'a> {
    Scope(&'a Scope),
    Ip(&'a IpRule),
    Policy(&'a Policy),
}

/// The Python gateway's checks after the key is known, in its order: the key's allowed paths,
/// the IP rules, then the policies.
fn refusal<'a>(snap: &'a Snapshot, n: &Native<'a>, path: &str, at: Duration) -> Option<Refusal<'a>> {
    if let Some(scope) = n.entry.scope.as_ref().filter(|s| !s.allows(path)) {
        return Some(Refusal::Scope(scope));
    }
    let tenant = n.entry.tenant.as_ref().map(|t| &*t.id);
    // A token is no key: the id its requests are counted under names no key's rules.
    let key_id = n.entry.token.is_none().then_some(&*n.entry.id);
    if let Some(rule) = snap.ip_rules.blocking(n.ip, tenant, key_id) {
        return Some(Refusal::Ip(rule));
    }
    if snap.policies.is_empty() {
        return None;
    }
    let subject = Subject {
        path,
        method: n.method,
        tenant: tenant.unwrap_or(""),
        ip: n.ip,
        micros_of_day: (at.as_secs() % 86_400) * 1_000_000 + u64::from(at.subsec_micros()),
    };
    govern::refusing(&snap.policies, &subject).map(Refusal::Policy)
}

/// The Python gateway's error envelope. Errors it raises put the timestamp before the details;
/// the upstream failures it answers put it after.
fn envelope(status: StatusCode, code: &str, message_json: &str, details: &str, t: &Trace, details_last: bool) -> Response<Body> {
    let (id, at) = (json(&t.request_id), timestamp(wall(), false));
    let body = if details_last {
        format!("{{\"error\":\"{code}\",\"message\":{message_json},\"request_id\":{id},\"timestamp\":\"{at}\",\"details\":{details}}}")
    } else {
        format!("{{\"error\":\"{code}\",\"message\":{message_json},\"request_id\":{id},\"details\":{details},\"timestamp\":\"{at}\"}}")
    };
    let mut resp = Response::new(full(body));
    *resp.status_mut() = status;
    let h = resp.headers_mut();
    h.insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
    h.insert("x-content-type-options", HeaderValue::from_static("nosniff"));
    resp
}

/// `{first, ...rest}`: a JSON object with one member put before those of another.
fn led_by(first: &str, rest: &str) -> String {
    match rest.get(1..).map(str::trim_start) {
        Some(tail) if tail != "}" && !tail.is_empty() => format!("{{{first},{tail}"),
        _ => format!("{{{first}}}"),
    }
}

fn forbidden(r: &Refusal, path: &str, ip: IpAddr, t: &Trace) -> Response<Body> {
    let (message, details) = match r {
        Refusal::Scope(scope) => (
            "\"This API key is not allowed to access this path\"",
            format!(
                "{{\"path\":{},\"allowed_paths\":{},\"hint\":\"The key was created with allowed_paths. Use a key whose scope covers this path.\"}}",
                json(path),
                scope.patterns_json
            ),
        ),
        Refusal::Ip(rule) => ("\"Request blocked by IP rule\"", led_by(&format!("\"ip\":\"{ip}\""), &rule.details_json)),
        Refusal::Policy(p) => (p.message_json.as_str(), p.details_json.clone()),
    };
    envelope(StatusCode::FORBIDDEN, "AUTHORIZATION_ERROR", message, &details, t, true)
}

/// A 429, for a key's own limit or, with `tenant_details`, its tenant's quota.
fn rate_limited(window: &str, limit: u64, retry_after: u32, tenant_details: Option<&str>, t: &Trace) -> Response<Body> {
    let limits = format!("\"limit\":{limit},\"window\":\"{window}\",\"retry_after\":{retry_after}");
    let details = led_by(&limits, tenant_details.unwrap_or("{}"));
    let message = format!("\"Rate limit exceeded: {limit} requests per {window}\"");
    let mut resp = envelope(StatusCode::TOO_MANY_REQUESTS, "RATE_LIMIT_EXCEEDED", &message, &details, t, true);
    resp.headers_mut().insert("retry-after", HeaderValue::from(retry_after));
    resp
}

/// The backend did not answer (refused, reset, or slower than the timeout).
fn backend_unavailable(t: &Trace) -> Response<Body> {
    envelope(
        StatusCode::BAD_GATEWAY,
        "BACKEND_ERROR",
        "\"Backend: Backend service unavailable\"",
        "{\"hint\":\"The upstream backend could not be reached. Check that the backend is running and reachable from the gateway.\",\"service\":\"Backend\"}",
        t,
        false,
    )
}

/// The bridge's call to the gRPC server failed with this status.
fn bridge_failed(code: u8, t: &Trace) -> Response<Body> {
    let details = format!("{{\"grpc_code\":\"StatusCode.{}\",\"service\":\"gRPC\",\"method\":\"SayHello\"}}", bridge::code_name(code));
    envelope(StatusCode::BAD_GATEWAY, "BACKEND_ERROR", "\"gRPC: gRPC backend call failed\"", &details, t, false)
}

/// The circuit breaker is open: the request was not sent.
fn circuit_open(t: &Trace) -> Response<Body> {
    envelope(
        StatusCode::SERVICE_UNAVAILABLE,
        "SERVICE_UNAVAILABLE",
        "\"Backend: Upstream service is temporarily unavailable (circuit open)\"",
        "{\"service\":\"Backend\"}",
        t,
        false,
    )
}

/// The control plane itself could not be reached; there is no gateway behind us to shape the answer.
fn control_unreachable() -> Response<Body> {
    let t = Trace { request_id: uuid::Uuid::new_v4().to_string(), traceparent: String::new() };
    let mut resp =
        envelope(StatusCode::BAD_GATEWAY, "BACKEND_ERROR", "\"Gateway: control plane unreachable\"", "{\"service\":\"Gateway\"}", &t, false);
    if let Ok(v) = HeaderValue::from_str(&t.request_id) {
        resp.headers_mut().insert("x-request-id", v);
    }
    resp
}

/// The client stopped sending its body part-way; there is no one left to answer properly.
fn body_unreadable() -> Response<Body> {
    let mut resp = Response::new(full("{\"detail\":\"There was an error parsing the body\"}"));
    *resp.status_mut() = StatusCode::BAD_REQUEST;
    resp.headers_mut().insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
    resp
}

/// The headers every answer made or passed on here carries.
fn stamp(resp: &mut Response<Body>, t: &Trace, seconds: f64, version: Option<&str>, announced: Option<&Notices>) {
    let h = resp.headers_mut();
    // Tells operators (and the conformance suite) which plane answered.
    h.insert("x-served-by", HeaderValue::from_static("dataplane"));
    if let Ok(v) = HeaderValue::from_str(&format!("{seconds:.3}")) {
        h.insert("x-response-time", v);
    }
    if let Some(v) = version.and_then(|v| HeaderValue::from_str(v).ok()) {
        h.insert("x-api-version", v);
    }
    // A version on its way out says so on every answer, over whatever the backend said.
    if let Some(notices) = announced {
        if let Some(v) = &notices.warning {
            h.insert("warning", v.clone());
        }
        if let Some(v) = &notices.sunset {
            h.insert("sunset", v.clone());
        }
        if notices.deprecation {
            h.insert("deprecation", HeaderValue::from_static("true"));
        }
    }
    if let Ok(v) = HeaderValue::from_str(&t.request_id) {
        h.insert("x-request-id", v);
    }
    if let Ok(v) = HeaderValue::from_str(&t.traceparent) {
        h.insert("traceparent", v);
    }
}

/// An answer made here for a request that never reached a backend or a key.
struct Answered<'a> {
    method: &'static str,
    ip: IpAddr,
    version: Option<&'a str>,
    /// A refusal: counted under a bounded path label, as the Python gateway counts its own.
    rejected: bool,
    /// The error code of the body, for the log row.
    error: Option<&'static str>,
}

/// Stamp an answer made here, and report and log it as the Python gateway records its own:
/// counted, and logged without a key.
fn finish<B>(w: &Worker, mut resp: Response<Body>, a: &Answered, req: &Request<B>, t: &Trace, started: Instant, at: Duration) -> Response<Body> {
    let st = &*w.shared;
    let path = decoded(req.uri().path()).unwrap_or(Cow::Borrowed(req.uri().path()));
    let path = &*path;
    let status = resp.status().as_u16();
    let seconds = started.elapsed().as_secs_f64();
    let bytes_out = resp.body().size_hint().exact().unwrap_or(0);
    let (_, snap) = w.snapshot();
    stamp(&mut resp, t, seconds, a.version, snap.api_versions.announced(a.version));
    let user_agent = req.headers().get(USER_AGENT).and_then(|v| v.to_str().ok());
    if let Some(reporter) = &st.reporter {
        let served = Served {
            method: a.method,
            path,
            status,
            seconds,
            key_id: "",
            tenant: None,
            rejected: a.rejected,
            error_code: None,
            error_type: None,
        };
        reporter.record(w.index, &served, at.as_secs(), || Row {
            request_id: t.request_id.clone(),
            method: a.method,
            path: path.to_owned(),
            client_ip: a.ip.to_string(),
            api_key_id: None,
            tenant_id: None,
            status,
            duration_ms: seconds * 1000.0,
            user_agent: user_agent.map(str::to_owned),
            error: a.error.map(str::to_owned),
        });
    }
    if let Some(log) = &st.access_log {
        log.write(accesslog::line(&accesslog::Entry {
            at,
            request_id: &t.request_id,
            traceparent: &t.traceparent,
            key_id: None,
            tenant: None,
            ip: a.ip,
            method: a.method,
            path,
            query: req.uri().query().unwrap_or(""),
            status,
            bytes: bytes_out,
            seconds,
            user_agent,
            referrer: req.headers().get(REFERER).and_then(|v| v.to_str().ok()),
        }));
    }
    resp
}

/// Answer a request that carries no credential that works.
fn unauthenticated<B>(w: &Worker, u: &Unauthenticated, req: &Request<B>, started: Instant, at: Duration) -> Response<Body> {
    let t = trace_for(req.headers());
    let mut resp = envelope(StatusCode::UNAUTHORIZED, "AUTHENTICATION_ERROR", &u.wording.message_json, &u.wording.details_json, &t, true);
    if let Some(pages) = u.pages {
        resp = as_page(pages, resp);
    }
    let a = Answered { method: u.method, ip: u.ip, version: u.version, rejected: true, error: Some("AUTHENTICATION_ERROR") };
    finish(w, resp, &a, req, &t, started, at)
}

fn announce(st: &State, backend: &Backend, cfg: &breaker::Config, tr: breaker::Transition) {
    eprintln!("dataplane: circuit breaker for {} is {} (was {})", backend.upstream.text, tr.state.name(), tr.previous.name());
    if let Some(reporter) = &st.reporter {
        reporter.event(Event::CircuitBreaker {
            state: tr.state.name(),
            previous_state: tr.previous.name(),
            backend_url: backend.upstream.text.clone(),
            failure_count: tr.failures,
            failure_threshold: cfg.threshold,
            opened_at: wall().as_secs_f64(),
            timeout_seconds: cfg.timeout_secs,
        });
    }
}

/// The key's own limit that refuses this request, if one does: window, limit, seconds to wait.
fn key_limit(snap: &Snapshot, entry: &KeyEntry, now: u64) -> Option<(&'static str, u32, u32)> {
    if !snap.rate_limit_enabled {
        None
    } else if !entry.minute.allow(entry.per_minute, 60, now as u32) {
        Some(("minute", entry.per_minute, 60))
    } else if !entry.hour.allow(entry.per_hour, 3600, now as u32) {
        Some(("hour", entry.per_hour, 3600))
    } else {
        None
    }
}

/// An HTTP/2 request names its host in the address; put it where an HTTP/1 request has it, so
/// that everything after this reads the two alike and the Python gateway is told the same host.
fn as_http1_names_it<B>(req: &mut Request<B>) {
    let Some(authority) = req.uri().authority().cloned() else { return };
    if !req.headers().contains_key(HOST) {
        if let Ok(host) = HeaderValue::from_str(authority.as_str()) {
            req.headers_mut().insert(HOST, host);
        }
    }
    let mut parts = std::mem::take(req.uri_mut()).into_parts();
    (parts.scheme, parts.authority) = (None, None);
    *req.uri_mut() = Uri::from_parts(parts).unwrap_or_else(|_| Uri::from_static("/"));
}

pub async fn handle(w: Arc<Worker>, peer: SocketAddr, mut req: Request<Incoming>) -> Result<Response<Body>, Infallible> {
    let started = Instant::now();
    let st = &*w.shared;
    if req.version() == Version::HTTP_2 {
        as_http1_names_it(&mut req);
    }
    // The control plane's internal endpoints are never reachable from clients.
    // However the path is escaped: the Python gateway decodes it before routing it.
    if unescaped(req.uri().path()).starts_with(b"/internal/") {
        let mut resp = Response::new(full("{\"detail\":\"Not Found\"}"));
        *resp.status_mut() = StatusCode::NOT_FOUND;
        resp.headers_mut().insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
        return Ok(resp);
    }
    let (generation, snap) = w.snapshot();
    let at = wall();
    let now = at.as_secs();
    // The gateway's own health endpoints, where this side can speak for it.
    let link = st.sync.as_deref();
    if let Some(own) = own(&snap, &req, || link.map(Link::up).unwrap_or(true), || link.and_then(Link::readiness)) {
        if let Some(resp) = answer_own(&w, &snap, &own, peer, &req, started, at) {
            return Ok(resp);
        }
    }
    // What a web page in another origin is told. Where this side cannot say, the request goes
    // to the Python gateway further down, so nothing is lost by not knowing here.
    let granted = cross_origin(&snap, req.headers()).ok().flatten();
    // Fail closed: if the snapshot is not known to be current, the Python gateway decides.
    let trusted = st.sync.as_ref().map(|s| s.fresh()).unwrap_or(true);
    if trusted && req.headers().contains_key(UPGRADE) {
        if let Some(r) = relay(&snap, peer, &req, at).or_else(|| agent_relay(&snap, &req)) {
            if let Some(entry) = r.entry.filter(|e| e.last_used.load(Ordering::Relaxed) != now) {
                entry.last_used.store(now, Ordering::Relaxed);
            }
            match relay_websocket(&w, &r, req).await {
                Ok(resp) => return Ok(resp),
                Err(back) => req = back,
            }
        }
    }
    let mut key_in_authorization = false;
    let decision = if is_grpc(&req) {
        match admit_grpc(&snap, peer, &req, now, trusted) {
            Ok(call) => {
                key_in_authorization = call.key_in_authorization;
                Decision::Native(call.native)
            }
            Err(refusal) => return Ok(grpc_refused(&w, &refusal, peer, &req, started, at)),
        }
    } else if trusted {
        decide(&snap, peer, &req, now, |digest| st.denied.lock().unwrap().holds(digest, generation, started))
    } else {
        Decision::Ask(None)
    };
    let n = match decision {
        Decision::Native(n) => n,
        Decision::Agent(a) => return Ok(serve_agent(&w, &a, granted.as_ref(), peer, req, started, at).await),
        Decision::Refuse(u) => return Ok(unauthenticated(&w, &u, &req, started, at)),
        Decision::Preflight(p) => {
            let t = trace_for(req.headers());
            let mut resp = Response::new(full(p.body));
            *resp.status_mut() = if p.allowed { StatusCode::OK } else { StatusCode::BAD_REQUEST };
            for (name, value) in &p.headers {
                if let (Ok(name), Ok(value)) = (HeaderName::from_bytes(name.as_bytes()), HeaderValue::from_str(value)) {
                    resp.headers_mut().insert(name, value);
                }
            }
            resp.headers_mut().insert(CONTENT_TYPE, HeaderValue::from_static("text/plain; charset=utf-8"));
            let a = Answered { method: "OPTIONS", ip: p.ip, version: p.version, rejected: false, error: None };
            return Ok(finish(&w, resp, &a, &req, &t, started, at));
        }
        Decision::Options(o) => {
            let t = trace_for(req.headers());
            let mut resp = Response::new(full(Bytes::new()));
            for (name, value) in OPTIONS_ANSWER {
                resp.headers_mut().insert(name, HeaderValue::from_static(value));
            }
            // The CORS middleware writes what it grants this origin over the handler's own.
            if let Some(granted) = &granted {
                granted.onto(&mut resp);
            }
            let a = Answered { method: "OPTIONS", ip: o.ip, version: o.version, rejected: false, error: None };
            return Ok(finish(&w, resp, &a, &req, &t, started, at));
        }
        Decision::Ask(credential) => {
            let (resp, refused) = ask_control(&w, peer, req.map(Either::Left)).await;
            if let Some(digest) = credential.filter(|_| refused && resp.status() == StatusCode::UNAUTHORIZED) {
                st.denied.lock().unwrap().learn(digest, generation, Instant::now());
            }
            return Ok(resp);
        }
    };
    let entry = n.entry;
    if entry.last_used.load(Ordering::Relaxed) != now {
        entry.last_used.store(now, Ordering::Relaxed);
    }
    let t = trace_for(req.headers());
    let uri = req.uri().clone();
    let path = decoded(uri.path()).unwrap_or(Cow::Borrowed(uri.path()));
    let path = &*path;
    let tenant = entry.tenant.as_deref();
    let user_agent = (st.reporter.is_some() || st.access_log.is_some()).then(|| req.headers().get(USER_AGENT).cloned()).flatten();
    let referrer = st.access_log.as_ref().and_then(|_| req.headers().get(REFERER).cloned());
    let cb = snap.circuit_breaker;
    let backend = n.backend.unwrap_or(&st.backend);

    let refused = refusal(&snap, &n, path, at);
    let mut req = req.map(Either::Left);
    // Where the Python gateway looks inside the body, so does this side, before anything is
    // counted. A body that is certain to pass goes on; anything else is the Python gateway's to
    // answer, in its own words, so the request is handed over whole.
    let grpc = n.protocol == Protocol::Grpc;
    if refused.is_none() && !grpc && snap.boundary.reads_body(n.method, path) {
        match req.body().size_hint().exact() {
            Some(0) => {}
            Some(len) if len <= snap.boundary.max_body(n.method, path) => {
                let (parts, body) = req.into_parts();
                let Ok(read) = body.collect().await else { return Ok(body_unreadable()) };
                let bytes = read.to_bytes();
                let verdict = snap.boundary.check(n.method, path, &bytes);
                req = Request::from_parts(parts, full(bytes));
                if verdict == Verdict::AskPython {
                    return Ok(to_control(&w, peer, req).await);
                }
            }
            // No length given, or too long to hold here.
            _ => return Ok(to_control(&w, peer, req).await),
        }
    }

    // An answer whose body rules change may have to be shown to the Python gateway, which this
    // side can only do when it is linked to it.
    if w.shared.token.is_none() && n.shaping.response.iter().any(|a| a.changes_body()) {
        return Ok(to_control(&w, peer, req).await);
    }

    // Rules that change fields of the body: it is read here whole, before anything is counted,
    // and written again as the Python gateway would write it. If that is not certain, or the
    // body is long or of no stated length, the request is handed over as it came. The Python
    // gateway reads a body for these methods only.
    let mut rewritten = false;
    if refused.is_none() && matches!(n.method, "POST" | "PUT" | "PATCH") && n.shaping.request.iter().any(|a| a.changes_body()) {
        match req.body().size_hint().exact() {
            Some(0) => {}
            Some(len) if len <= RULE_BODY_MAX => {
                let (mut parts, body) = req.into_parts();
                let Ok(read) = body.collect().await else { return Ok(body_unreadable()) };
                let bytes = read.to_bytes();
                match reshape(&bytes, &n.shaping.request) {
                    Ok(None) => req = Request::from_parts(parts, full(bytes)),
                    Ok(Some(body)) => {
                        parts.headers.insert(CONTENT_LENGTH, HeaderValue::from(body.len()));
                        rewritten = true;
                        req = Request::from_parts(parts, full(body));
                    }
                    Err(()) => return Ok(to_control(&w, peer, Request::from_parts(parts, full(bytes))).await),
                }
            }
            _ => return Ok(to_control(&w, peer, req).await),
        }
    }

    // A message to publish is read here whole, before anything is counted. Unless what the
    // Python gateway would publish for it is certain, and this side's connection to the broker
    // stands, the request is handed over as it came.
    let (mut publish, mut subscribe) = (None, None);
    if refused.is_none() && n.protocol == Protocol::Mqtt {
        let ready = snap.mqtt.as_ref().is_some_and(|broker| st.mqtt.serves(broker));
        match req.body().size_hint().exact() {
            Some(len) if ready && len <= PUBLISH_BODY_MAX => {
                let (parts, body) = req.into_parts();
                let Ok(read) = body.collect().await else { return Ok(body_unreadable()) };
                let bytes = read.to_bytes();
                match path == SUBSCRIBE {
                    true => subscribe = pyjson::subscribe(&bytes),
                    false => publish = pyjson::publish(&bytes),
                }
                req = Request::from_parts(parts, full(bytes));
            }
            _ => {}
        }
        if publish.is_none() && subscribe.is_none() {
            return Ok(to_control(&w, peer, req).await);
        }
    }

    // JSON for the bridge is read here whole, before anything is counted. Unless what the Python
    // gateway would do for it is certain, the request is handed over as it came.
    let mut bridged = None;
    if refused.is_none() && n.protocol == Protocol::Bridge {
        if let (Some(bridge), Some(len)) = (snap.bridge.as_ref(), req.body().size_hint().exact()) {
            if len <= PUBLISH_BODY_MAX {
                let (parts, body) = req.into_parts();
                let Ok(read) = body.collect().await else { return Ok(body_unreadable()) };
                let bytes = read.to_bytes();
                bridged = bridge::plan(&bytes, bridge).map(|plan| (plan, bridge));
                req = Request::from_parts(parts, full(bytes));
            }
        }
        if bridged.is_none() {
            return Ok(to_control(&w, peer, req).await);
        }
    }

    let (mut ticket, mut rejected, mut error_type) = (None, false, None);
    let (mut resp, error) = if let Some(r) = refused {
        rejected = true;
        (forbidden(&r, path, n.ip, &t), Some("AUTHORIZATION_ERROR"))
    } else if let Some((window, limit, retry_after)) = key_limit(&snap, entry, now) {
        rejected = true;
        if let Some(reporter) = &st.reporter {
            reporter.rate_limited(&entry.id, entry.token.as_deref(), window, limit, path, n.ip);
        }
        (rate_limited(window, u64::from(limit), retry_after, None, &t), Some("RATE_LIMIT_EXCEEDED"))
    } else {
        match tenant.map(|tn| tn.usage.admit(tn.minute, tn.hour, now)).transpose() {
            Err(over) => {
                rejected = true;
                let details = tenant.map(|tn| tn.details_json.as_str());
                (rate_limited(over.window, over.limit, over.retry_after, details, &t), Some("RATE_LIMIT_EXCEEDED"))
            }
            Ok(admitted) => {
                ticket = admitted;
                if let Some(message) = publish.take() {
                    // Answered as the Python gateway answers: once the message is on its way.
                    let mut resp = Response::new(full(format!("{{\"status\":\"published\",\"topic\":{}}}", pyjson::quoted(&message.topic))));
                    resp.headers_mut().insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
                    st.mqtt.publish(message);
                    (resp, None)
                } else if let Some(wanted) = subscribe.take() {
                    // Answered as the Python gateway answers: once the broker is being asked.
                    let mut resp = Response::new(full(format!("{{\"status\":\"subscribed\",\"topic\":{}}}", pyjson::quoted(&wanted.topic))));
                    resp.headers_mut().insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
                    st.mqtt.subscribe(wanted);
                    (resp, None)
                } else if let Some((plan, bridge)) = bridged.take() {
                    let said = |json: Vec<u8>| {
                        let mut resp = Response::new(full(json));
                        resp.headers_mut().insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
                        resp
                    };
                    let sent = |method: &str, name: &str, seconds: u64| bridge::call(&bridge.uri, method, name, seconds).map(|call| w.grpc.request(call.map(full)));
                    match plan {
                        bridge::Plan::Answer(json) => (said(json), None),
                        bridge::Plan::Hello(name) => match sent("SayHello", &name, bridge::UNARY_SECONDS) {
                            Some(sent) => match bridge::hello(sent).await {
                                Ok(message) => (said(bridge::greeted(&message).into_bytes()), None),
                                Err(code) => (bridge_failed(code, &t), Some("BACKEND_ERROR")),
                            },
                            None => (backend_unavailable(&t), Some("BACKEND_ERROR")),
                        },
                        bridge::Plan::Hellos(name) => match sent("SayManyHellos", &name, bridge::STREAM_SECONDS) {
                            // Answered at once, as the Python gateway answers: the lines follow as the call yields them.
                            Some(sent) => {
                                let mut resp = Response::new(Either::Right(Either::Right(bridge::Lines::new(sent))));
                                resp.headers_mut().insert(CONTENT_TYPE, bridge::ndjson());
                                (resp, None)
                            }
                            None => (backend_unavailable(&t), Some("BACKEND_ERROR")),
                        },
                    }
                } else if !backend.breaker.allow(&cb, Instant::now()) {
                    (circuit_open(&t), Some("SERVICE_UNAVAILABLE"))
                } else {
                    let asked = match (n.rewritten.as_deref(), n.route) {
                        (Some(rewritten), _) => rewritten,
                        (None, Some(route)) => route.rewrite.as_deref().unwrap_or(path),
                        (None, None) => upstream_path(path, n.protocol),
                    };
                    // Asked for as it came when nothing about it changes on the way.
                    let untouched = backend.upstream.prefix.is_empty() && asked == uri.path() && plain_path(asked);
                    let wire = (!untouched && !grpc).then(|| sent_path(&format!("{}{asked}", backend.upstream.prefix)));
                    let native = Sent {
                        trace: &t,
                        version: n.told,
                        notices: snap.api_versions.announced(n.told),
                        inject_request_id: snap.inject_request_id,
                        protocol: n.route.map(|r| r.protocol.as_str()).unwrap_or(n.protocol.name()),
                        path: wire.as_deref(),
                    };
                    let limit = n.route.and_then(DynamicRoute::timeout).unwrap_or(snap.upstream_timeout);
                    // gRPC has a client of its own in the Python gateway, which sends every line on.
                    if !grpc {
                        shape_request(&mut req, &n.shaping.request, rewritten, Some(&native));
                    }
                    let answer = match grpc {
                        // Refused by the Python gateway's HTTP client before it connects anywhere.
                        _ if backend.never_sent => Err(Unanswered::Unsupported),
                        true => forward_grpc(&w, &backend.upstream, peer, req, &t, &snap.key_header, key_in_authorization).await,
                        false => forward(&w, &backend.upstream, peer, req, Some(native), Some(limit)).await,
                    };
                    match answer {
                        Ok(mut resp) => {
                            if let Some(tr) = backend.breaker.success(&cb) {
                                announce(st, backend, &cb, tr);
                            }
                            if n.shaping.response.iter().any(|a| a.changes_body()) {
                                match answer_with_body_rules(&w, resp, &n.shaping.response, n.method, path).await {
                                    Ok(resp) => (resp, None),
                                    Err(()) => {
                                        error_type = Some("RemoteProtocolError");
                                        (backend_unavailable(&t), Some("BACKEND_ERROR"))
                                    }
                                }
                            } else {
                                if !n.shaping.response.is_empty() {
                                    shape_answer(&mut resp, &n.shaping.response);
                                }
                                (resp, None)
                            }
                        }
                        Err(unanswered) => {
                            if let Some(tr) = backend.breaker.failure(&cb, Instant::now()) {
                                announce(st, backend, &cb, tr);
                            }
                            error_type = Some(unanswered.name());
                            (backend_unavailable(&t), Some("BACKEND_ERROR"))
                        }
                    }
                }
            }
        }
    };

    // To a browser, an error made here is a page.
    if let (true, Some(_), Some(pages)) = (n.html && !grpc, error, snap.error_pages.as_ref()) {
        resp = as_page(pages, resp);
    }
    let status = resp.status().as_u16();
    let seconds = started.elapsed().as_secs_f64();
    let bytes_out = resp
        .headers()
        .get(CONTENT_LENGTH)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.parse().ok())
        .or_else(|| resp.body().size_hint().exact())
        .unwrap_or(0);
    stamp(&mut resp, &t, seconds, n.version, snap.api_versions.announced(n.version));
    if let Some(v) = tenant.and_then(|tn| tn.header.clone()) {
        resp.headers_mut().insert("x-tenant-id", v);
    }
    // An answered request is usage, whatever the answer; a refused one never is.
    if let Some(ticket) = ticket {
        ticket.complete(status, bytes_out);
    }
    let user_agent = user_agent.as_ref().and_then(|v| v.to_str().ok());
    if let Some(reporter) = &st.reporter {
        let served = Served {
            method: n.method,
            path,
            status,
            seconds,
            key_id: &entry.id,
            tenant: tenant.map(|tn| &*tn.id),
            rejected,
            error_code: error.filter(|_| status >= 500),
            error_type,
        };
        reporter.record(w.index, &served, now, || Row {
            request_id: t.request_id.clone(),
            method: n.method,
            path: path.to_owned(),
            client_ip: n.ip.to_string(),
            // A token is no row of the keys table, and the request log names none for it.
            api_key_id: entry.token.is_none().then(|| entry.id.to_string()),
            tenant_id: tenant.map(|tn| tn.id.to_string()),
            status,
            duration_ms: seconds * 1000.0,
            user_agent: user_agent.map(str::to_owned),
            error: error.map(str::to_owned),
        });
    }
    if let Some(log) = &st.access_log {
        log.write(accesslog::line(&accesslog::Entry {
            at,
            request_id: &t.request_id,
            traceparent: &t.traceparent,
            key_id: entry.token.as_deref().or(Some(&entry.id)),
            tenant: tenant.map(|tn| &*tn.id),
            ip: n.ip,
            method: n.method,
            path,
            query: uri.query().unwrap_or(""),
            status,
            bytes: bytes_out,
            seconds,
            user_agent,
            referrer: referrer.as_ref().and_then(|v| v.to_str().ok()),
        }));
    }
    // An answer from the backend, or the 502 or 503 that stands in for one, passes through the
    // Python gateway's CORS middleware. Its refusals (401, 403, 429) are made outside it.
    if let Some(granted) = granted.as_ref().filter(|_| !rejected && !grpc) {
        granted.onto(&mut resp);
    }
    // A gRPC client reads a refusal in gRPC's own form; it was counted and logged above as the
    // HTTP answer it stands for.
    if let Some(error) = error.filter(|_| grpc) {
        resp = GrpcRefusal::for_error(error).onto(resp);
    }
    Ok(resp)
}

/// The upstream gave no response.
#[derive(Debug, PartialEq)]
enum Unanswered {
    /// No connection could be made.
    Refused,
    /// Slower than the timeout to start answering.
    TimedOut,
    /// Connected, then failed before a response arrived.
    Broken,
    /// Never sent: the backend's address is in a scheme the Python gateway's HTTP client does not speak.
    Unsupported,
}

impl Unanswered {
    /// The name the Python gateway's metrics give this failure (its HTTP client's error class).
    fn name(&self) -> &'static str {
        match self {
            Unanswered::Refused => "ConnectError",
            Unanswered::TimedOut => "ReadTimeout",
            Unanswered::Broken => "RemoteProtocolError",
            Unanswered::Unsupported => "UnsupportedProtocol",
        }
    }
}

/// What a native request carries to the backend.
struct Sent<'a> {
    trace: &'a Trace,
    version: Option<&'a str>,
    /// What that version announces, when it is on its way out.
    notices: Option<&'a Notices>,
    inject_request_id: bool,
    /// The protocol name the backend is told.
    protocol: &'a str,
    /// The whole path to ask the backend for (its own prefix included), when it is not the one
    /// the client asked for.
    path: Option<&'a str>,
}

/// Hand a request to the Python gateway as it came; its answer is the answer. Also says whether
/// the Python gateway marked the answer as its refusal of the credential itself (a note for this
/// side only, taken off before the answer goes to the client).
async fn ask_control(w: &Worker, peer: SocketAddr, req: Request<Body>) -> (Response<Body>, bool) {
    match forward(w, &w.shared.control, peer, req, None, None).await {
        Ok(mut resp) => {
            let refused = resp.headers_mut().remove("x-dataplane-auth").is_some();
            (resp, refused)
        }
        Err(_) => (control_unreachable(), false),
    }
}

async fn to_control(w: &Worker, peer: SocketAddr, req: Request<Body>) -> Response<Body> {
    ask_control(w, peer, req).await.0
}

/// Forward to `to`. With `native`, the Python gateway's upstream headers are reproduced.
/// Without, the request is handed to the control plane as it came, plus the client's address.
/// `timeout` bounds the wait for the response to start.
async fn forward(
    w: &Worker,
    to: &Upstream,
    peer: SocketAddr,
    mut req: Request<Body>,
    native: Option<Sent<'_>>,
    timeout: Option<Duration>,
) -> Result<Response<Body>, Unanswered> {
    let mut parts = std::mem::take(req.uri_mut()).into_parts();
    parts.scheme = Some(to.scheme.clone());
    parts.authority = Some(to.authority.clone());
    let asked = parts.path_and_query.as_ref();
    let target = match native.as_ref().and_then(|sent| sent.path) {
        Some(path) => Some(match asked.and_then(|p| p.query()) {
            Some(query) => format!("{path}?{query}"),
            None => path.to_owned(),
        }),
        None if asked.is_none() || !to.prefix.is_empty() => Some(format!("{}{}", to.prefix, asked.map(|p| p.as_str()).unwrap_or("/"))),
        None => None,
    };
    if let Some(target) = target {
        parts.path_and_query = Some(target.parse().map_err(|_| Unanswered::Broken)?);
    }
    *req.uri_mut() = Uri::from_parts(parts).map_err(|_| Unanswered::Broken)?;
    *req.version_mut() = Version::HTTP_11;
    let native_answer = native.is_some();
    // A request to change protocol (a WebSocket handshake) keeps the headers that ask for it, and
    // this side's end of the connection is held ready for the tunnel.
    let asked_for = req.headers().get(UPGRADE).cloned();
    let client_end = asked_for.as_ref().map(|_| hyper::upgrade::on(&mut req));
    let stamped = req.extensions().get::<Stamped>().is_some();
    let headers = req.headers_mut();
    for h in HOP_BY_HOP {
        headers.remove(h);
    }
    if let Some(protocol) = asked_for {
        headers.insert(CONNECTION, HeaderValue::from_static("upgrade"));
        headers.insert(UPGRADE, protocol);
    }
    match native {
        Some(sent) => {
            headers.remove(HOST);
            // Written already, with the rules, where a request's headers were changed by them.
            if !stamped {
                stamp_request(headers, &sent);
            }
        }
        None => {
            let xff = match headers.get("x-forwarded-for").and_then(|v| v.to_str().ok()) {
                Some(prior) => format!("{prior}, {}", peer.ip()),
                None => peer.ip().to_string(),
            };
            if let Ok(v) = HeaderValue::from_str(&xff) {
                headers.insert("x-forwarded-for", v);
            }
        }
    }
    let sent = w.send(to, req);
    let answer = match timeout {
        Some(limit) => tokio::time::timeout(limit, sent).await.map_err(|_| Unanswered::TimedOut)?,
        None => sent.await,
    };
    match answer {
        Ok(resp) => {
            if let Some(client_end) = client_end.filter(|_| resp.status() == StatusCode::SWITCHING_PROTOCOLS) {
                return Ok(tunnel(client_end, resp));
            }
            let (mut rp, rb) = resp.into_parts();
            for h in HOP_BY_HOP {
                rp.headers.remove(h);
            }
            // A backend cannot leave notes for this side; the Python gateway drops this one too.
            if native_answer {
                rp.headers.remove("x-dataplane-auth");
            }
            Ok(Response::from_parts(rp, Either::Left(rb)))
        }
        Err(e) if e.is_connect() => Err(Unanswered::Refused),
        Err(_) => Err(Unanswered::Broken),
    }
}

/// Headers that describe the connection or the message's framing, not the request. Of one of
/// these given more than once the Python gateway sends a backend one line; of any other, every
/// line (`repeated_headers`).
const ONE_LINE: [&str; 10] = ["host", "content-length", "connection", "keep-alive", "transfer-encoding", "upgrade", "te", "trailer", "proxy-authorization", "proxy-authenticate"];

/// Whether a request gives more than once a header the Python gateway sends on one line of.
fn framed_twice(headers: &HeaderMap) -> bool {
    headers.len() != headers.keys_len() && ONE_LINE.iter().any(|name| headers.get_all(*name).iter().nth(1).is_some())
}

/// Take out of a request's headers every line but the first of each name given more than once,
/// and say what they were. The Python gateway's rules read a request's headers from a dictionary
/// of one value to a name, the first line's; the lines are kept beside it.
fn take_repeated(headers: &mut HeaderMap) -> Vec<(HeaderName, Vec<HeaderValue>)> {
    if headers.len() == headers.keys_len() {
        return Vec::new();
    }
    let repeated: Vec<(HeaderName, Vec<HeaderValue>)> = headers
        .keys()
        .filter(|name| headers.get_all(*name).iter().nth(1).is_some())
        .map(|name| (name.clone(), headers.get_all(name).iter().cloned().collect()))
        .collect();
    for (name, values) in &repeated {
        headers.insert(name.clone(), values[0].clone());
    }
    repeated
}

/// The upstream agreed to change protocol: pass its answer on and, once the client has it, carry
/// bytes both ways until either side closes. What is spoken inside (WebSocket frames, pings,
/// compression) is between the two ends.
fn tunnel(client_end: hyper::upgrade::OnUpgrade, mut resp: Response<Incoming>) -> Response<Body> {
    let upstream_end = hyper::upgrade::on(&mut resp);
    tokio::spawn(async move {
        // The upstream's end is ready already; the client's is once it has been sent the answer.
        if let (Ok(upstream), Ok(client)) = (upstream_end.await, client_end.await) {
            let _ = tokio::io::copy_bidirectional(&mut TokioIo::new(client), &mut TokioIo::new(upstream)).await;
        }
    });
    let (mut parts, _) = resp.into_parts();
    let protocol = parts.headers.remove(UPGRADE);
    for h in HOP_BY_HOP {
        parts.headers.remove(h);
    }
    parts.headers.insert(CONNECTION, HeaderValue::from_static("upgrade"));
    if let Some(protocol) = protocol {
        parts.headers.insert(UPGRADE, protocol);
    }
    Response::from_parts(parts, full(Bytes::new()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_path_belongs_to_the_protocol_the_python_router_gives_it() {
        for (path, protocol) in [
            ("/api/users", Some(Protocol::Http)),
            ("/mqtt/publish", Some(Protocol::Mqtt)),
            ("/mqtt/subscribe", Some(Protocol::Mqtt)),
            ("/api/v1/orders/7", Some(Protocol::Http)),
            ("/mcp", Some(Protocol::Mcp)),
            ("/mcp/tools", Some(Protocol::Mcp)),
            ("/api/mcp/x", Some(Protocol::Mcp)),
            ("/api/mcpx/y", Some(Protocol::Mcp)),
            ("/a2a/v1/message:send", Some(Protocol::A2a)),
            ("/api/a2a", Some(Protocol::A2a)),
            ("/agui", Some(Protocol::AgUi)),
            ("/api/agui/run", Some(Protocol::AgUi)),
            ("/acp/runs", Some(Protocol::Acp)),
            ("/api/acp/agents", Some(Protocol::Acp)),
            ("/graphql", Some(Protocol::GraphQl)),
            ("/api/graphql/batch", Some(Protocol::GraphQl)),
            ("/graphqlx", Some(Protocol::GraphQl)),
            ("/grpc/x", Some(Protocol::Bridge)),
            ("/api/grpc", Some(Protocol::Bridge)),
            ("/grpcx", Some(Protocol::Bridge)),
            ("/grpc/graphql", Some(Protocol::Bridge)),
        ] {
            assert_eq!(classify(path, None), protocol, "{path}");
        }
        for path in [
            "/admin/keys", "/oauth2/token", "/health", "/docs", "/", "/api", "/apix/y", "/rest/x", "/users",
            "/mqtt/publish/", "/mqtt/subscribe/", "/mqtt/subscriptions", "/mqtt", "/api/mqtt/publish", "/api/mqtt/subscribe", "/api/mqtt/x", "/ws/x", "/websocket", "/wss", "/ws/graphql",
            "/api/ws/x", "/api/agents/a", "/agents/a/mcp",
        ] {
            assert_eq!(classify(path, None), None, "{path}");
        }
    }

    #[test]
    fn a_path_with_no_prefix_is_plain_http_once_the_control_plane_names_its_own() {
        let own: Vec<String> = ["/admin", "/health", "/oauth2", "/metrics", "/.well-known/agent-card.json"].map(String::from).to_vec();
        for path in ["/", "/users", "/users/7", "/rest/x", "/http/x", "/api", "/apix/y", "/agents", "/v/x", "/heal", "/.well-known/other"] {
            assert_eq!(classify(path, Some(&own)), Some(Protocol::Http), "{path}");
        }
        // The control plane's own, by how they begin, and what it kept before.
        for path in [
            "/health", "/health/detailed", "/healthz", "/admin/keys", "/oauth2/token", "/metrics", "/.well-known/agent-card.json",
            "/mqtt/topics", "/ws/x", "/wss", "/websocket", "/api/ws/x", "/api/agents/a",
        ] {
            assert_eq!(classify(path, Some(&own)), None, "{path}");
        }
        assert_eq!(classify("/mcp/x", Some(&own)), Some(Protocol::Mcp));
        assert_eq!(classify("/graphql", Some(&own)), Some(Protocol::GraphQl));
    }

    #[test]
    fn a_preflight_is_answered_as_the_cors_middleware_answers_it() {
        let rules = |json: &str| serde_json::from_str::<crate::snapshot::CrossOrigin>(json).unwrap();
        let listed = rules(
            r#"{"any_origin":false,"origins":["https://app.example"],"credentials":true,"preflight":{
                "headers":[["Vary","Origin"],["Access-Control-Allow-Methods","GET, POST"],["Access-Control-Max-Age","600"],
                           ["Access-Control-Allow-Headers","Accept, Accept-Language, Content-Language, Content-Type, X-API-Key"],
                           ["Access-Control-Allow-Credentials","true"]],
                "explicit_origin":true,"methods":["GET","POST"],"all_headers":false,
                "allow_headers":["accept","accept-language","content-language","content-type","x-api-key"]}}"#,
        );
        let ask = |rules: &crate::snapshot::CrossOrigin, sent: &[(&str, &str)]| {
            let mut h = HeaderMap::new();
            for (name, value) in sent {
                h.append(HeaderName::from_bytes(name.as_bytes()).unwrap(), HeaderValue::from_str(value).unwrap());
            }
            preflight(rules, &h)
        };
        let named = |headers: &[(String, String)], name: &str| headers.iter().find(|(n, _)| n == name).map(|(_, v)| v.clone());
        let (ok, headers, body) = ask(&listed, &[("origin", "https://app.example"), ("access-control-request-method", "POST"),
                                                  ("access-control-request-headers", "X-API-Key , content-type")]).unwrap();
        assert!(ok && body == "OK");
        assert_eq!(named(&headers, "Access-Control-Allow-Origin").as_deref(), Some("https://app.example"));
        assert_eq!(headers[0], ("Vary".to_owned(), "Origin".to_owned()));
        // Everything wrong at once is said at once, in this order, and the origin is not named back.
        let (ok, headers, body) = ask(&listed, &[("origin", "https://evil.example"), ("access-control-request-method", "DELETE"),
                                                  ("access-control-request-headers", "x-other")]).unwrap();
        assert!(!ok && body == "Disallowed CORS origin, method, headers" && named(&headers, "Access-Control-Allow-Origin").is_none());
        // The method is compared as written.
        assert_eq!(ask(&listed, &[("origin", "https://app.example"), ("access-control-request-method", "post")]).unwrap().2, "Disallowed CORS method");
        let any = rules(
            r#"{"any_origin":true,"origins":[],"credentials":false,"preflight":{
                "headers":[["Access-Control-Allow-Origin","*"],["Access-Control-Allow-Methods","GET"],["Access-Control-Max-Age","600"]],
                "explicit_origin":false,"methods":["GET"],"all_headers":true,"allow_headers":[]}}"#,
        );
        let (ok, headers, _) = ask(&any, &[("origin", "https://anyone.example"), ("access-control-request-method", "GET"),
                                            ("access-control-request-headers", "X-Whatever, Y")]).unwrap();
        assert!(ok);
        assert_eq!(named(&headers, "Access-Control-Allow-Origin").as_deref(), Some("*"));
        assert_eq!(named(&headers, "Access-Control-Allow-Headers").as_deref(), Some("X-Whatever, Y"), "named back as asked");
        // Not a preflight, or one this side will not read for the Python gateway.
        assert!(ask(&listed, &[("origin", "https://app.example")]).is_none());
        assert!(ask(&listed, &[("access-control-request-method", "GET")]).is_none());
        assert!(ask(&listed, &[("origin", "https://app.example"), ("access-control-request-method", "GET"), ("access-control-request-method", "POST")]).is_none());
        let silent = rules(r#"{"any_origin":true,"origins":[],"credentials":false}"#);
        assert!(ask(&silent, &[("origin", "https://app.example"), ("access-control-request-method", "GET")]).is_none());
    }

    #[test]
    fn an_escaped_path_is_read_decoded_and_written_again_as_httpx_writes_it() {
        for (raw, read) in [
            ("/api/users", Some("/api/users")),
            ("/api/a%20b", Some("/api/a b")),
            ("/api/caf%C3%A9", Some("/api/caf\u{e9}")),
            ("/%61pi/x", Some("/api/x")),
            ("/api/a%2Fb", Some("/api/a/b")),
            ("/api/100%", Some("/api/100%")),
            ("/api/%zz%4", Some("/api/%zz%4")),
            ("/api/%2525", Some("/api/%25")),
            ("/api/a%3Fb", None),
            ("/api/a%23b", None),
            ("/api/a%00b", None),
            ("/api/a%7Fb", None),
            ("/api/%FF", None),
            ("/api/caf\u{e9}", None),
        ] {
            assert_eq!(decoded(raw).as_deref(), read, "{raw}");
        }
        for (whole, sent) in [
            ("/api/users", "/api/users"),
            ("/api/a b", "/api/a%20b"),
            ("/api/caf\u{e9}", "/api/caf%C3%A9"),
            ("/api/a|b", "/api/a%7Cb"),
            ("/api/it's;[1]@:x", "/api/it's;[1]@:x"),
            // Made only of safe characters and whole escapes: left alone. Otherwise a `%` is escaped too.
            ("/api/%41", "/api/%41"),
            ("/api/100%", "/api/100%25"),
            ("/api/a b%20c", "/api/a%20b%2520c"),
            ("/api/a/../b", "/api/b"),
            ("/api/./a/.", "/api/a"),
            ("/base/../../x", "/x"),
            ("/..", "/"),
            ("/a/...", "/a/..."),
        ] {
            assert_eq!(sent_path(whole), sent, "{whole}");
        }
        assert!(plain_path("/api/users/7") && !plain_path("/api/a b") && !plain_path("/api/./a") && !plain_path("/api/.hidden"));
    }

    #[test]
    fn acp_and_ag_ui_backends_see_their_own_paths() {
        for (path, protocol, sent) in [
            ("/acp", Protocol::Acp, "/"),
            ("/acp/runs/7", Protocol::Acp, "/runs/7"),
            ("/api/acp/runs", Protocol::Acp, "/runs"),
            ("/acpx/runs", Protocol::Acp, "/acpx/runs"),
            ("/agui/", Protocol::AgUi, "/"),
            ("/api/agui", Protocol::AgUi, "/"),
            ("/agui/run", Protocol::AgUi, "/run"),
            ("/mcp/tools", Protocol::Mcp, "/mcp/tools"),
            ("/a2a/x", Protocol::A2a, "/a2a/x"),
            ("/api/users", Protocol::Http, "/api/users"),
            ("/graphql", Protocol::GraphQl, "/graphql"),
            ("/api/graphql", Protocol::GraphQl, "/graphql"),
            ("/api/graphql/batch", Protocol::GraphQl, "/graphql/batch"),
            ("/graphqlx", Protocol::GraphQl, "/graphqlx"),
        ] {
            assert_eq!(upstream_path(path, protocol), sent, "{path}");
        }
    }

    #[test]
    fn an_answer_is_streamed_when_python_would_stream_it() {
        let asked = |path: &str, method: &str, accept: Option<&str>, query: Option<&str>| {
            let mut headers = HeaderMap::new();
            if let Some(accept) = accept {
                headers.insert(ACCEPT, HeaderValue::from_str(accept).unwrap());
            }
            streamed(path, method, &headers, query)
        };
        let events = Some("application/json, Text/Event-Stream;q=0.9");
        assert_eq!(asked("/a2a/v1/message:stream", "POST", None, None), Some(true));
        assert_eq!(asked("/a2a/v1/message%3Astream", "POST", None, None), Some(false));
        assert_eq!(asked("/api/users", "POST", events, Some("sse=1&stream=1")), Some(false));
        // AG-UI: events accepted, or asked for in the query, where the last word counts.
        assert_eq!(asked("/agui/run", "POST", None, None), Some(false));
        assert_eq!(asked("/api/agui/run", "GET", events, None), Some(true));
        for (query, is) in [("sse=1", true), ("sse=true", true), ("a=1&sse=yes", true), ("sse", false),
                            ("sse=TRUE", false), ("xsse=1", false), ("sse=1=1", false), ("&&sse=1", true), ("stream=1", false)] {
            assert_eq!(asked("/agui/run", "POST", None, Some(query)), Some(is), "{query}");
        }
        assert_eq!(asked("/agui/run", "POST", None, Some("sse=0&sse=1")), None);
        assert_eq!(asked("/agui/run", "POST", None, Some("sse=%31")), None);
        assert_eq!(asked("/agui/run", "POST", None, Some("q=a+b")), None);
        assert_eq!(asked("/agui/run", "POST", events, Some("q=a+b")), Some(true));
        // ACP: a run, posted by a caller that accepts events or asked for as a stream.
        assert_eq!(asked("/acp/runs", "POST", events, None), Some(true));
        assert_eq!(asked("/acp/runs", "GET", events, None), Some(false));
        assert_eq!(asked("/acp/runs/42", "GET", None, Some("stream=yes")), Some(true));
        assert_eq!(asked("/acp/agents", "POST", events, Some("stream=1")), Some(false));
        assert_eq!(asked("/acp/runs", "GET", None, Some("stream=%31")), None);
    }

    #[test]
    fn an_answers_body_is_changed_only_where_python_would_change_it_the_same() {
        let snap = Snapshot::from_json(
            r#"{"keys":[],"transform_rules":[{"all":true,"native":true,"request":null,
                "response":{"body_rename":[["user","meta.user"]],"body_remove":["secret"],"body_set":[["seen","true"]]}},
                {"all":true,"native":true,"request":null,"response":{"set":[["x-a","1"]]}}]}"#,
        )
        .unwrap();
        let shaping = snap.shaping("GET", "/x", "/x").unwrap();
        let made = |body: &[u8]| match reshape_answer(body, &shaping.response) {
            AnswerBody::AsItCame => "as it came".to_owned(),
            AnswerBody::Unsure => "unsure".to_owned(),
            AnswerBody::Changed(body) => String::from_utf8(body).unwrap(),
        };
        assert_eq!(made(br#" {"user":"ana","secret":1,"n":1.50}"#), r#"{"n": 1.5, "meta": {"user": "ana"}, "seen": true}"#);
        assert_eq!(made(b"{}"), r#"{"seen": true}"#);
        // What is no object goes on as it came: nothing, other JSON, no JSON at all.
        for body in [&b""[..], b"[1, {\"secret\": 2}]", b"5", b"null", b"plain words", b"\xef\xbb\xbf{}", b"\x0b{}"] {
            assert_eq!(made(body), "as it came", "{body:?}");
        }
        // What the Python gateway reads its own way is its to say.
        for body in [&b"{\"a\": \"caf\xe9\"}"[..], b"\"{\\\"a\\\": 1}\"", b"{\"a\": NaN}", b"{\"a\": 1,", b"{'a': 1}", b"\xff"] {
            assert_eq!(made(body), "unsure", "{body:?}");
        }
        // Nothing left in the object: the body goes on as it came.
        let only = Snapshot::from_json(r#"{"keys":[],"transform_rules":[{"all":true,"native":true,"request":null,"response":{"body_remove":["secret"]}}]}"#).unwrap();
        let shaping = only.shaping("GET", "/x", "/x").unwrap();
        assert!(matches!(reshape_answer(br#"{"secret": 1}"#, &shaping.response), AnswerBody::AsItCame));
        assert!(matches!(reshape_answer(br#"{"secret": 1, "a": 2}"#, &shaping.response), AnswerBody::Changed(body) if body == br#"{"a": 2}"#));
    }

    #[test]
    fn a_version_in_the_path_is_recognised_as_python_recognises_it() {
        for (path, named) in [("/v1", ("1", "")), ("/v1/", ("1", "/")), ("/v2/runs", ("2", "/runs")), ("/v10.5/x", ("10.5", "/x")), ("/v0.0", ("0.0", ""))] {
            assert_eq!(version_named(path), Some(named), "{path}");
        }
        for path in ["/", "/runs", "/v", "/vx/1", "/v1x", "/v1.2.3", "/version", "/v1./x", "/v.5/x", "v1/x", "/a/v1/x"] {
            assert_eq!(version_named(path), None, "{path}");
        }
    }

    #[test]
    fn a_backend_is_told_the_version_its_path_names() {
        let snap = |strip: bool| {
            Snapshot::from_json(&format!(
                r#"{{"keys":[],"api_versions":{{"default":"1","strip_prefix":{strip},"versions":[
                {{"version":"1"}},{{"version":"2"}},{{"version":"3","native_ok":false}}]}}}}"#
            ))
            .unwrap()
        };
        let (kept, taken_off) = (snap(false), snap(true));
        // No version in the path: what the headers or the default gave.
        assert_eq!(told_version(&kept, "/runs", Some("1")), Some((Some("1"), None)));
        assert_eq!(told_version(&taken_off, "/version/2", None), Some((None, None)));
        // One the gateway knows: that one, and the path without it where versions are taken off.
        assert_eq!(told_version(&kept, "/v2/runs", Some("1")), Some((Some("2"), None)));
        assert_eq!(told_version(&taken_off, "/v2/runs", Some("1")), Some((Some("2"), Some("/runs"))));
        assert_eq!(told_version(&taken_off, "/v2", Some("1")), Some((Some("2"), Some("/"))));
        assert_eq!(told_version(&taken_off, "/v2/", Some("1")), Some((Some("2"), Some("/"))));
        // One it does not know: nothing is told, and the path stays.
        assert_eq!(told_version(&taken_off, "/v9/runs", Some("1")), Some((None, None)));
        assert_eq!(told_version(&kept, "/v2.0/runs", Some("1")), Some((None, None)));
        // One that adds more than its number, or digits Python reads and this side does not.
        assert_eq!(told_version(&kept, "/v3/runs", Some("1")), None);
        assert_eq!(told_version(&kept, "/v\u{662}/runs", Some("1")), None);
        assert_eq!(told_version(&kept, "/v1\u{662}", Some("1")), None);
        assert_eq!(told_version(&kept, "/v1/\u{662}", Some("1")), Some((Some("1"), None)));
    }

    #[test]
    fn timestamps_have_the_python_shapes() {
        let at = Duration::new(1_700_000_000, 123_456_000);
        assert_eq!(timestamp(at, false), "2023-11-14T22:13:20.123456");
        assert_eq!(timestamp(at, true), "2023-11-14T22:13:20.123Z");
        assert_eq!(timestamp(Duration::new(951_782_400, 0), true), "2000-02-29T00:00:00.000Z");
        // Python leaves a zero fraction out.
        assert_eq!(timestamp(Duration::new(951_782_400, 0), false), "2000-02-29T00:00:00");
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
    fn what_a_version_announces_is_written_to_the_backend_and_on_the_answer() {
        let snap = Snapshot::from_json(r#"{"keys":[],"api_versions":{"default":"1","versions":[
            {"version":"1"},
            {"version":"7","notices":{"warning":"299 - \"API version 7 is deprecated\"","sunset":"Fri, 02 Jan 2099 03:04:05 -0000","deprecation":true}},
            {"version":"8","notices":{"warning":null,"sunset":null,"deprecation":true}},
            {"version":"9","notices":{"warning":"not\nwritable","sunset":null,"deprecation":true}},
            {"version":"6","native_ok":false,"notices":{"warning":"w","sunset":null,"deprecation":false}}]}}"#).unwrap();
        let versions = &snap.api_versions;
        assert_eq!(versions.native_ok.get("7"), Some(&true));
        // A notice that cannot be a header, or a version that is not this side's: nothing is announced here.
        assert_eq!((versions.native_ok.get("9"), versions.announced(Some("9"))), (Some(&false), None));
        assert_eq!((versions.native_ok.get("6"), versions.announced(Some("6"))), (Some(&false), None));
        assert!(versions.announced(Some("1")).is_none() && versions.announced(None).is_none());
        let read = |headers: &HeaderMap, name: &str| headers.get_all(name).iter().map(|v| v.to_str().unwrap().to_owned()).collect::<Vec<_>>();
        let trace = Trace { request_id: "id".to_owned(), traceparent: "tp".to_owned() };
        let came = || {
            let mut headers = HeaderMap::new();
            headers.insert("warning", HeaderValue::from_static("mine"));
            headers
        };
        // To the backend, with no rule: beside the caller's own line, and after it.
        let sent = Sent { trace: &trace, version: Some("7"), notices: versions.announced(Some("7")), inject_request_id: false, protocol: "http", path: None };
        let mut headers = came();
        stamp_request(&mut headers, &sent);
        assert_eq!(read(&headers, "warning"), ["mine", "299 - \"API version 7 is deprecated\""]);
        assert_eq!(read(&headers, "sunset"), ["Fri, 02 Jan 2099 03:04:05 -0000"]);
        assert!(read(&headers, "deprecation").is_empty());
        // With a rule: a line the rule left under the gateway's own spelling is taken over.
        let rule: Actions = serde_json::from_str(r#"{"set":[["Warning","the rule's"],["sunset","the rule's"]]}"#).unwrap();
        let mut headers = came();
        assert!(shape_headers(&mut headers, &[&rule], Some(&sent), &[]));
        assert_eq!(read(&headers, "warning"), ["mine", "299 - \"API version 7 is deprecated\""]);
        assert_eq!(read(&headers, "sunset"), ["the rule's", "Fri, 02 Jan 2099 03:04:05 -0000"]);
        // A deprecation date alone tells the backend nothing.
        let quiet = Sent { version: Some("8"), notices: versions.announced(Some("8")), ..Sent { trace: &trace, version: None, notices: None, inject_request_id: false, protocol: "http", path: None } };
        let mut headers = HeaderMap::new();
        stamp_request(&mut headers, &quiet);
        assert!(read(&headers, "warning").is_empty() && read(&headers, "sunset").is_empty());
        // On the answer: over whatever the backend said.
        let mut resp = Response::new(full(Bytes::new()));
        resp.headers_mut().append("warning", HeaderValue::from_static("the backend's"));
        resp.headers_mut().append("warning", HeaderValue::from_static("and another"));
        stamp(&mut resp, &trace, 0.001, Some("7"), versions.announced(Some("7")));
        assert_eq!(read(resp.headers(), "warning"), ["299 - \"API version 7 is deprecated\""]);
        assert_eq!(read(resp.headers(), "sunset"), ["Fri, 02 Jan 2099 03:04:05 -0000"]);
        assert_eq!(read(resp.headers(), "deprecation"), ["true"]);
        let mut resp = Response::new(full(Bytes::new()));
        resp.headers_mut().append("warning", HeaderValue::from_static("the backend's"));
        stamp(&mut resp, &trace, 0.001, Some("8"), versions.announced(Some("8")));
        assert_eq!((read(resp.headers(), "warning"), read(resp.headers(), "deprecation")), (vec!["the backend's".to_owned()], vec!["true".to_owned()]));
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
    fn a_browser_is_served_here_once_the_pages_for_its_errors_are_known() {
        let browser = |snap: &Snapshot, headers: &[(&str, &str)]| {
            let mut b = Request::builder().uri("/api/users").header("accept", "text/html,application/xhtml+xml");
            for (k, v) in headers {
                b = b.header(*k, *v);
            }
            match decide(snap, peer(), &b.body(()).unwrap(), 100, |_| false) {
                Decision::Native(n) => format!("native, pages: {}", n.html),
                Decision::Refuse(u) => format!("refused, pages: {}", u.pages.is_some()),
                _ => "python".into(),
            }
        };
        assert_eq!(browser(&governed("", ""), &[("x-api-key", KEY)]), "python");
        let pages = r#","error_pages":{"401:10":"<p>@@message@@</p>"},"refusals":{"missing_key":{"message_json":"\"API key required\"","details_json":"{}"},"invalid_key":{"message_json":"\"Invalid\"","details_json":"{}"}}"#;
        let snap = governed(pages, "");
        assert_eq!(browser(&snap, &[("x-api-key", KEY)]), "native, pages: true");
        assert_eq!(browser(&snap, &[]), "refused, pages: true");
        // A script in a page is answered in JSON, as anyone else.
        assert_eq!(browser(&snap, &[("x-api-key", KEY), ("x-requested-with", "XMLHttpRequest")]), "native, pages: false");
    }

    #[test]
    fn an_error_becomes_the_page_for_its_status_and_shape() {
        let pages: HashMap<String, String> = [
            ("429:11", "<b>@@code@@</b><p>@@message@@</p><i>@@hint@@</i><u>987654321</u><script>n=987654321</script><code>@@request-id@@</code>"),
            ("429:01", "<p>@@message@@</p><u>987654321</u><code>@@request-id@@</code>"),
            ("403:10", "<b>@@code@@</b><p>@@message@@</p><i>@@hint@@</i><code>@@request-id@@</code>"),
        ]
        .into_iter()
        .map(|(k, v)| (k.to_owned(), v.to_owned()))
        .collect();
        let mut named = HeaderMap::new();
        named.insert("x-request-id", HeaderValue::from_static("id-<1>-@@message@@"));
        let t = trace_for(&named);
        let page = |resp: Response<Body>| {
            let resp = as_page(&pages, resp);
            (resp.status().as_u16(), resp.headers()["content-type"].to_str().unwrap().to_owned(), String::from_utf8(bytes_of(resp).to_vec()).unwrap())
        };
        let limited = rate_limited("minute", 3, 60, Some(r#"{"tenant":"t1","hint":"Wait & retry"}"#), &t);
        assert!(limited.headers().contains_key("retry-after"));
        let (status, kind, body) = page(limited);
        assert_eq!((status, kind.as_str()), (429, "text/html; charset=utf-8"));
        assert_eq!(
            body,
            "<b>RATE_LIMIT_EXCEEDED</b><p>Rate limit exceeded: 3 requests per minute</p><i>Wait &amp; retry</i><u>60</u><script>n=60</script><code>id-&lt;1&gt;-@@message@@</code>"
        );
        // Without a hint, the page without one.
        assert!(page(rate_limited("hour", 9, 3600, None, &t)).2.starts_with("<p>Rate limit exceeded: 9 requests per hour</p><u>3600</u>"));
        let scoped = governed("", r#","allowed_paths":{"rules":[["under","/api/users"]],"patterns_json":"[\"/api/users\"]"}"#);
        let scope = scoped.keys.values().next().unwrap().scope.as_ref().unwrap();
        let body = page(forbidden(&Refusal::Scope(scope), "/api/orders", "10.0.0.1".parse().unwrap(), &t)).2;
        assert!(body.starts_with("<b>AUTHORIZATION_ERROR</b><p>This API key is not allowed to access this path</p><i>The key was created"), "{body}");
        // A shape there is no page for stays as it was.
        let (status, kind, _) = page(backend_unavailable(&t));
        assert_eq!((status, kind.as_str()), (502, "application/json"));
    }

    const KEY: &str = "sk-test";

    fn governed(extra: &str, key_extra: &str) -> Snapshot {
        let hex: String = hash_key(KEY.as_bytes()).iter().map(|b| format!("{b:02x}")).collect();
        let mut snap = Snapshot::from_json(&format!(
            r#"{{"keys":[{{"hash":"{hex}","id":"k1","rate_limit_per_minute":100,"rate_limit_per_hour":1000{key_extra}}}]{extra}}}"#
        ))
        .unwrap();
        Backends::default().resolve(&mut snap);
        snap
    }

    /// The request as served here, or `None` for anything else.
    fn route<'a>(snap: &'a Snapshot, peer: SocketAddr, req: &Request<()>, now: u64) -> Option<Native<'a>> {
        match decide(snap, peer, req, now, |_| false) {
            Decision::Native(n) => Some(n),
            _ => None,
        }
    }

    fn request(method: &str, uri: &str) -> Request<()> {
        Request::builder().method(method).uri(uri).header("x-api-key", KEY).body(()).unwrap()
    }

    fn peer() -> SocketAddr {
        "203.0.113.7:4000".parse().unwrap()
    }

    #[test]
    fn a_known_key_on_a_plain_path_is_served_here() {
        let snap = governed("", "");
        let n = route(&snap, peer(), &request("GET", "/api/users?x=1"), 0).unwrap();
        assert_eq!((&*n.entry.id, n.method, n.version, n.ip.to_string().as_str()), ("k1", "GET", Some("1"), "203.0.113.7"));
    }

    #[test]
    fn a_protocol_is_served_here_only_with_a_backend_this_side_can_reach() {
        let snap = governed(r#","backends":{"mcp":"http://127.0.0.1:9201/","a2a":"http://127.0.0.1:9201","ag_ui":"https://agui.example","acp":null}"#, "");
        let n = route(&snap, peer(), &request("POST", "/mcp/rpc"), 0).unwrap();
        assert_eq!((n.protocol, n.backend.unwrap().upstream.text.as_str()), (Protocol::Mcp, "http://127.0.0.1:9201"));
        let a2a = route(&snap, peer(), &request("GET", "/api/a2a/tasks"), 0).unwrap();
        assert!(std::ptr::eq(n.backend.unwrap(), a2a.backend.unwrap()), "one address, one breaker");
        assert!(route(&snap, peer(), &request("POST", "/agui/run"), 0).is_none(), "https: Python's");
        assert!(route(&snap, peer(), &request("POST", "/acp/runs"), 0).is_none(), "not configured: Python's");
        let http = route(&snap, peer(), &request("GET", "/api/users"), 0).unwrap();
        assert!(http.protocol == Protocol::Http && http.backend.is_none());
        assert!(route(&governed("", ""), peer(), &request("POST", "/mcp/rpc"), 0).is_none(), "no backends in the snapshot");
    }

    #[test]
    fn a_stripped_path_that_names_a_version_is_that_versions() {
        let versions = r#","api_versions":{"default":"1","versions":[{"version":"1"},{"version":"2"},{"version":"3","native_ok":false}]}"#;
        let snap = governed(&format!(r#","backends":{{"acp":"http://127.0.0.1:9201"}}{versions}"#), "");
        let told = |uri: &str| route(&snap, peer(), &request("GET", uri), 0).map(|n| (n.version, n.told, n.rewritten));
        assert_eq!(told("/acp/runs"), Some((Some("1"), Some("1"), None)));
        assert_eq!(told("/acp/v2/runs"), Some((Some("1"), Some("2"), None)));
        assert_eq!(told("/api/acp/v9"), Some((Some("1"), None, None)));
        assert_eq!(told("/acp/v3/runs"), None, "a version that adds more than its number: Python's");
        // The request's own path is not what the backend is asked for, and names no version.
        assert_eq!(told("/api/v3/users"), Some((Some("1"), Some("1"), None)));
        let taken_off = governed(&format!(r#","backends":{{"acp":"http://127.0.0.1:9201"}}{}"#, versions.replace(r#""default":"1""#, r#""default":"1","strip_prefix":true"#)), "");
        let n = route(&taken_off, peer(), &request("GET", "/acp/v2/runs"), 0).unwrap();
        assert_eq!((n.told, n.rewritten.as_deref()), (Some("2"), Some("/runs")));
    }

    #[test]
    fn control_plane_prefixes_win() {
        let snap = governed(r#","fallthrough_prefixes":["/api/custom"]"#, "");
        assert!(route(&snap, peer(), &request("GET", "/api/custom/x"), 0).is_none());
        assert!(route(&snap, peer(), &request("GET", "/api/other"), 0).is_some());
    }

    #[test]
    fn an_options_request_that_is_no_preflight_is_answered_here_without_a_key() {
        let snap = governed(r#","cross_origin":{"any_origin":false,"origins":["https://app.example"],"credentials":true}"#, "");
        let asks = |headers: &[(&str, &str)]| {
            let mut req = Request::builder().method("OPTIONS").uri("/api/users");
            for (name, value) in headers {
                req = req.header(*name, *value);
            }
            outcome(&snap, &req.body(()).unwrap(), 0, false)
        };
        // With or without a key, from a page or not: the proxy handler answers before it looks.
        assert_eq!(asks(&[]), "options");
        assert_eq!(asks(&[("x-api-key", "sk-nope")]), "options");
        assert_eq!(asks(&[("origin", "https://app.example")]), "options");
        assert_eq!(asks(&[("access-control-request-method", "POST")]), "options");
        // A preflight is the CORS middleware's, and an origin given twice the Python gateway's.
        assert!(!matches!(asks(&[("origin", "https://app.example"), ("access-control-request-method", "POST")]).as_str(), "options"));
        assert_eq!(asks(&[("origin", "https://app.example"), ("origin", "https://other.example")]), "ask");
        // The gateway's own paths, and agents, are not this answer's.
        for uri in ["/admin/api-keys", "/agents/hiring/a", "/metrics"] {
            let req = Request::builder().method("OPTIONS").uri(uri).body(()).unwrap();
            assert_eq!(outcome(&snap, &req, 0, false), "ask", "{uri}");
        }
    }

    #[test]
    fn what_this_side_cannot_be_sure_of_goes_to_python() {
        let snap = governed("", "");
        for (method, uri) in [
            ("GET", "/api/a%3Fb"),
            ("GET", "/api/a%23b"),
            ("GET", "/api/a%00b"),
            ("GET", "/api/%C3%28"),
            ("GET", "/%61dmin/api-keys"),
            ("GET", "/api/caf\u{e9}"),
            ("PROPFIND", "/api/users"),
            ("GET", "http://other.example/api/users"),
            ("GET", "/admin/api-keys"),
        ] {
            assert!(route(&snap, peer(), &request(method, uri), 0).is_none(), "{method} {uri}");
        }
        let unknown = Request::builder().uri("/api/users").header("x-api-key", "sk-other").body(()).unwrap();
        assert!(route(&snap, peer(), &unknown, 0).is_none());
        let keyless = Request::builder().uri("/api/users").body(()).unwrap();
        assert!(route(&snap, peer(), &keyless, 0).is_none());
    }

    #[test]
    fn the_client_address_comes_from_a_trusted_proxy() {
        let snap = governed(r#","trusted_proxies":["203.0.113.0/24"]"#, "");
        let mut req = request("GET", "/api/users");
        req.headers_mut().insert("x-forwarded-for", HeaderValue::from_static("198.51.100.9, 203.0.113.1"));
        assert_eq!(route(&snap, peer(), &req, 0).unwrap().ip.to_string(), "198.51.100.9");
        req.headers_mut().append("x-forwarded-for", HeaderValue::from_static("10.0.0.1"));
        assert!(route(&snap, peer(), &req, 0).is_none(), "two headers: Python decides");
    }

    fn refused(snap: &Snapshot, req: &Request<()>, at: Duration) -> Option<String> {
        let n = route(snap, peer(), req, at.as_secs()).expect("native");
        let t = Trace { request_id: "r\"1".into(), traceparent: String::new() };
        let r = refusal(snap, &n, req.uri().path(), at)?;
        let resp = forbidden(&r, req.uri().path(), n.ip, &t);
        assert_eq!(resp.status(), StatusCode::FORBIDDEN);
        Some(match &r {
            Refusal::Scope(_) => "scope".into(),
            Refusal::Ip(rule) => format!("ip {}", rule.details_json),
            Refusal::Policy(p) => format!("policy {}", p.details_json),
        })
    }

    #[test]
    fn a_key_only_reaches_its_allowed_paths() {
        let snap = governed("", r#","allowed_paths":{"rules":[["under","/api/users"]],"patterns_json":"[\"/api/users\"]"}"#);
        let at = Duration::from_secs(0);
        assert_eq!(refused(&snap, &request("GET", "/api/users/7"), at), None);
        assert_eq!(refused(&snap, &request("GET", "/api//users"), at), None);
        assert_eq!(refused(&snap, &request("GET", "/api/usersx"), at).as_deref(), Some("scope"));
        assert_eq!(refused(&snap, &request("GET", "/api/orders"), at).as_deref(), Some("scope"));
    }

    #[test]
    fn ip_rules_come_before_policies_and_the_first_matching_policy_decides() {
        let rules = r#","ip_rules":[{"scope":"global","scope_id":null,"allow":false,"net":"203.0.113.0/24","details_json":"{\"rule_id\":\"r1\",\"scope\":\"global\"}"}]"#;
        let policies = r#","policies":[
            {"path":"/api/open/*","methods":[],"tenants":[],"cidrs":null,"time":null,"deny":false,"message_json":"\"\"","details_json":"{}"},
            {"path":"/api/*","methods":["DELETE"],"tenants":[],"cidrs":null,"time":null,"deny":true,"message_json":"\"no deletes\"","details_json":"{\"policy_id\":\"p1\"}"}]"#;
        let at = Duration::from_secs(0);
        let both = governed(&format!("{rules}{policies}"), "");
        assert!(refused(&both, &request("DELETE", "/api/users"), at).unwrap().starts_with("ip "));
        let snap = governed(policies, "");
        assert_eq!(refused(&snap, &request("DELETE", "/api/users"), at).as_deref(), Some(r#"policy {"policy_id":"p1"}"#));
        assert_eq!(refused(&snap, &request("GET", "/api/users"), at), None);
        assert_eq!(refused(&snap, &request("DELETE", "/api/open/x"), at), None, "an earlier allow wins");
    }

    #[test]
    fn a_policy_window_is_read_in_utc() {
        let policies = r#","policies":[{"path":null,"methods":[],"tenants":[],"cidrs":null,"time":[60,120],"deny":true,"message_json":"\"closed\"","details_json":"{\"policy_id\":\"p2\"}"}]"#;
        let snap = governed(policies, "");
        let day = 1_700_006_400 - 1_700_006_400 % 86_400;
        assert!(refused(&snap, &request("GET", "/api/x"), Duration::from_secs(day + 90 * 60)).is_some());
        assert!(refused(&snap, &request("GET", "/api/x"), Duration::from_secs(day + 121 * 60)).is_none());
    }

    fn bytes_of(resp: Response<Body>) -> Bytes {
        let Either::Right(Either::Left(body)) = resp.into_body() else { panic!("made here") };
        let mut body = std::pin::pin!(body);
        let waker = std::task::Waker::noop();
        let mut cx = std::task::Context::from_waker(waker);
        let std::task::Poll::Ready(Some(Ok(frame))) = body.as_mut().poll_frame(&mut cx) else { panic!("a full body is ready") };
        frame.into_data().unwrap()
    }

    fn body_of(resp: Response<Body>) -> serde_json::Value {
        serde_json::from_slice(&bytes_of(resp)).unwrap()
    }

    #[test]
    fn refusals_are_valid_json_in_the_python_order() {
        let t = Trace { request_id: "r\"1".into(), traceparent: String::new() };
        let rule = IpRule { allow: false, net: None, details_json: r#"{"rule_id":"r1","scope":"tenant"}"#.into() };
        let resp = forbidden(&Refusal::Ip(&rule), "/api/x", "10.1.2.3".parse().unwrap(), &t);
        let v = body_of(resp);
        assert_eq!(v["request_id"], "r\"1");
        assert_eq!(v["details"], serde_json::json!({"ip":"10.1.2.3","rule_id":"r1","scope":"tenant"}));
        assert_eq!(v["message"], "Request blocked by IP rule");

        let resp = rate_limited("minute", 5, 60, Some(r#"{"tenant_id":"acme","quota":{"requests_per_minute":5}}"#), &t);
        assert_eq!(resp.headers()["retry-after"], "60");
        let v = body_of(resp);
        assert_eq!(v["message"], "Rate limit exceeded: 5 requests per minute");
        assert_eq!(
            serde_json::to_string(&v["details"]).unwrap().len(),
            r#"{"limit":5,"window":"minute","retry_after":60,"tenant_id":"acme","quota":{"requests_per_minute":5}}"#.len()
        );
        assert_eq!(body_of(rate_limited("hour", 9, 3600, None, &t))["details"], serde_json::json!({"limit":9,"window":"hour","retry_after":3600}));
        assert_eq!(led_by("\"a\":1", "{}"), "{\"a\":1}");
    }

    const REFUSALS: &str = r#","refusals":{"missing_key":{"message_json":"\"API key required\"","details_json":"{\"hint\":\"h1\"}"},
        "invalid_key":{"message_json":"\"Invalid or inactive API key.\"","details_json":"{\"hint\":\"h2\"}"}}"#;

    fn with_headers(headers: &[(&str, &str)]) -> Request<()> {
        let mut b = Request::builder().uri("/api/users");
        for (name, value) in headers {
            b = b.header(*name, *value);
        }
        b.body(()).unwrap()
    }

    /// "native", "ask", "learn" (ask, and remember a refusal) or the message of the 401 made here.
    fn outcome(snap: &Snapshot, req: &Request<()>, now: u64, denied: bool) -> String {
        match decide(snap, peer(), req, now, |_| denied) {
            Decision::Native(_) => "native".into(),
            Decision::Agent(_) => "agent".into(),
            Decision::Ask(None) => "ask".into(),
            Decision::Ask(Some(_)) => "learn".into(),
            Decision::Refuse(u) => u.wording.message_json.clone(),
            Decision::Preflight(p) => format!("preflight: {}", p.body),
            Decision::Options(_) => "options".into(),
        }
    }

    const TOKEN: &str = "tok-abc";

    /// A snapshot that also lists `TOKEN`, good until second 1000.
    fn with_token(extra: &str) -> Snapshot {
        let hex: String = hash_key(TOKEN.as_bytes()).iter().map(|b| format!("{b:02x}")).collect();
        governed(
            &format!(
                r#"{REFUSALS},"tokens":[{{"hash":"{hex}","id":"non-database-key","name":"oauth2:idp","expires_at":1000,"rate_limit_per_minute":7,"rate_limit_per_hour":70}}]{extra}"#
            ),
            "",
        )
    }

    #[test]
    fn an_oauth2_token_is_a_credential_in_authorization_only() {
        let snap = with_token("");
        for value in ["Bearer tok-abc", "bearer tok-abc", "BEARER tok-abc", "Bearer   tok-abc", "Bearer \ttok-abc \t"] {
            let req = with_headers(&[("authorization", value)]);
            let Decision::Native(n) = decide(&snap, peer(), &req, 0, |_| false) else { panic!("{value}") };
            assert_eq!((n.entry.token.as_deref(), &*n.entry.id, n.entry.per_minute, n.entry.per_hour), (Some("oauth2:idp"), "non-database-key", 7, 70));
        }
        // Not without the word, not in the key's own header, not twice the word, not another scheme.
        for headers in [
            vec![("authorization", TOKEN)],
            vec![("x-api-key", TOKEN)],
            vec![("authorization", "Bearer Bearer tok-abc")],
            vec![("authorization", "Basic tok-abc")],
            vec![("authorization", "Bearertok-abc")],
            vec![("authorization", "Bearer tok-abd")],
        ] {
            assert_eq!(outcome(&snap, &with_headers(&headers), 0, false), "learn", "{headers:?}");
        }
        // With a key header as well, the Python gateway says which of the two lets the request in.
        assert_eq!(outcome(&snap, &with_headers(&[("x-api-key", "sk-other"), ("authorization", "Bearer tok-abc")]), 0, false), "ask");
        // A key in the same header is still a key.
        let both = format!("Bearer {KEY}");
        let Decision::Native(n) = decide(&snap, peer(), &with_headers(&[("authorization", &both)]), 0, |_| false) else { panic!() };
        assert_eq!((n.entry.token.as_deref(), &*n.entry.id), (None, "k1"));
    }

    #[test]
    fn a_token_that_has_run_out_is_the_python_gateways_to_refuse_first() {
        let snap = with_token("");
        let req = with_headers(&[("authorization", "Bearer tok-abc")]);
        assert_eq!(outcome(&snap, &req, 999, false), "native");
        for now in [1000, 1001, 5000] {
            assert_eq!(outcome(&snap, &req, now, false), "learn", "{now}");
            assert_eq!(outcome(&snap, &req, now, true), "\"Invalid or inactive API key.\"", "{now}");
        }
    }

    #[test]
    fn a_token_is_served_here_under_the_rules_but_no_keys_rules() {
        let req = with_headers(&[("authorization", "Bearer tok-abc")]);
        let rule = r#","ip_rules":[{"scope":"global","allow":false,"net":"198.51.100.0/24","details_json":"{}"}]"#;
        let keyed = r#","ip_rules":[{"scope":"api_key","scope_id":"k9","allow":true,"net":"198.51.100.0/24","details_json":"{}"}]"#;
        let policy = r#","policies":[{"path":null,"methods":[],"tenants":[],"cidrs":null,"time":[60,120],"deny":true,"message_json":"\"closed\"","details_json":"{}"}]"#;
        for extra in [rule, keyed, policy] {
            let snap = with_token(extra);
            assert_eq!(outcome(&snap, &req, 0, false), "native", "{extra}");
            assert_eq!(outcome(&snap, &with_headers(&[("x-api-key", KEY)]), 0, false), "native", "{extra}");
        }
        let from = |addr: &str, headers: &[(&str, &str)], extra: &str| {
            let snap = with_token(extra);
            let peer: SocketAddr = format!("{addr}:4000").parse().unwrap();
            let Decision::Native(n) = decide(&snap, peer, &with_headers(headers), 0, |_| false) else { panic!("not served here") };
            refusal(&snap, &n, "/api/users", Duration::from_secs(90 * 60)).is_some()
        };
        let token: &[(&str, &str)] = &[("authorization", "Bearer tok-abc")];
        // A global rule blocks a token's caller as it blocks a key's.
        assert!(from("198.51.100.7", token, rule) && !from("203.0.113.9", token, rule));
        // A rule scoped to the id tokens are counted under is some key's, not theirs.
        let named = keyed.replace("k9", "non-database-key");
        assert!(!from("203.0.113.9", token, &named));
        // A policy applies whoever calls.
        let always = policy.replace("[60,120]", "[0,1440]");
        assert!(from("203.0.113.9", token, &always) && from("203.0.113.9", &[("x-api-key", KEY)], &always));
    }

    #[test]
    fn the_token_is_read_as_python_reads_it() {
        for (value, token) in [
            ("Bearer abc", Some("abc")),
            ("bEaReR  abc  ", Some("abc")),
            ("Bearer a b", Some("a b")),
            ("Bearer ", None),
            ("Bearer    ", None),
            ("Bearer", None),
            ("Bearerabc", None),
            ("Basic abc", None),
            ("abc", None),
            ("", None),
        ] {
            assert_eq!(bearer_token(value.as_bytes()), token.map(str::as_bytes), "{value:?}");
        }
    }

    #[test]
    fn a_key_is_read_from_its_header_or_from_authorization() {
        let snap = governed(REFUSALS, "");
        for headers in [
            vec![("x-api-key", KEY)],
            vec![("authorization", "Bearer sk-test")],
            vec![("authorization", "sk-test")],
            vec![("x-api-key", ""), ("authorization", "Bearer sk-test")],
            vec![("x-api-key", KEY), ("authorization", "Bearer something-else")],
            vec![("authorization", "Bearer Bearer sk-test")],
            vec![("authorization", "sk-Bearer test")],
        ] {
            assert_eq!(outcome(&snap, &with_headers(&headers), 0, false), "native", "{headers:?}");
        }
        // `bearer` in another case is not taken out, as in Python: this is not the key.
        assert_eq!(outcome(&snap, &with_headers(&[("authorization", "bearer sk-test")]), 0, false), "learn");
    }

    #[test]
    fn the_key_header_is_the_one_the_gateway_is_configured_with() {
        let snap = governed(&format!(r#","key_header":"X-Gateway-Key"{REFUSALS}"#), "");
        assert_eq!(outcome(&snap, &with_headers(&[("x-gateway-key", KEY)]), 0, false), "native");
        assert_eq!(outcome(&snap, &with_headers(&[("x-api-key", KEY)]), 0, false), "\"API key required\"");
    }

    #[test]
    fn a_request_without_a_credential_is_refused_here() {
        let snap = governed(REFUSALS, "");
        assert_eq!(outcome(&snap, &with_headers(&[]), 0, false), "\"API key required\"");
        assert_eq!(outcome(&snap, &with_headers(&[("x-api-key", "")]), 0, false), "\"API key required\"");
        // Without the wording (a plugin may authenticate some other way), the Python gateway answers.
        assert_eq!(outcome(&governed("", ""), &with_headers(&[]), 0, false), "ask");
        // What the Python gateway does not treat as a gateway request is not refused here either.
        for (method, uri) in [("GET", "/health"), ("GET", "/admin/api-keys"), ("OPTIONS", "/admin/api-keys"), ("GET", "/agents/a"), ("GET", "/api/a%3Fb"), ("GET", "/api/a%0Ab"), ("GET", "/api/%FF")] {
            let req = Request::builder().method(method).uri(uri).body(()).unwrap();
            assert_eq!(outcome(&snap, &req, 0, false), "ask", "{method} {uri}");
        }
        let browser = Request::builder().uri("/api/users").header("accept", "text/html").body(()).unwrap();
        assert_eq!(outcome(&snap, &browser, 0, false), "ask");
        let handshake = Request::builder().uri("/api/users").header("upgrade", "websocket").header("x-api-key", KEY).body(()).unwrap();
        assert_eq!(outcome(&snap, &handshake, 0, false), "ask");
        // A path the gateway answers itself without a key (its health check, moved under /api).
        let moved = governed(&format!(r#","fallthrough_prefixes":["/api/health"]{REFUSALS}"#), "");
        assert_eq!(outcome(&moved, &Request::builder().uri("/api/health").body(()).unwrap(), 0, false), "ask");
    }

    #[test]
    fn an_unknown_credential_is_refused_here_only_after_python_refused_it() {
        let snap = governed(REFUSALS, "");
        let unknown = with_headers(&[("x-api-key", "sk-other")]);
        assert_eq!(outcome(&snap, &unknown, 0, false), "learn");
        assert_eq!(outcome(&snap, &unknown, 0, true), "\"Invalid or inactive API key.\"");
        assert_eq!(outcome(&snap, &with_headers(&[("authorization", "Bearer sk-other")]), 0, true), "\"Invalid or inactive API key.\"");
        // With a second credential the Python gateway may still let the request in.
        let two = with_headers(&[("x-api-key", "sk-other"), ("authorization", "Bearer token")]);
        assert_eq!(outcome(&snap, &two, 0, true), "ask");
        // Beyond ASCII, and without the wording, it is not this side's call.
        assert_eq!(outcome(&snap, &with_headers(&[("x-api-key", "sk-caf\u{e9}")]), 0, true), "ask");
        assert_eq!(outcome(&governed("", ""), &unknown, 0, true), "ask");
    }

    #[test]
    fn the_same_text_in_another_header_is_another_credential() {
        let snap = governed(REFUSALS, "");
        let digest = |req: &Request<()>| match decide(&snap, peer(), req, 0, |_| false) {
            Decision::Ask(Some(d)) => d,
            _ => panic!("an unknown credential"),
        };
        let own = digest(&with_headers(&[("x-api-key", "token-1")]));
        assert_ne!(own, digest(&with_headers(&[("authorization", "token-1")])));
        assert_ne!(digest(&with_headers(&[("authorization", "Bearer token-1")])), digest(&with_headers(&[("authorization", "token-1")])));
        assert_eq!(own, digest(&with_headers(&[("x-api-key", "token-1")])));
    }

    #[test]
    fn an_expired_key_is_refused_here_once_it_is_clearly_past() {
        let snap = governed(REFUSALS, r#","expires_at":1000"#);
        let req = with_headers(&[("x-api-key", KEY)]);
        assert_eq!(outcome(&snap, &req, 999, false), "native");
        assert_eq!(outcome(&snap, &req, 1000, false), "ask", "at the boundary the clocks may differ");
        assert_eq!(outcome(&snap, &req, 1002, false), "ask");
        assert_eq!(outcome(&snap, &req, 1003, false), "\"Invalid or inactive API key.\"");
        let two = with_headers(&[("x-api-key", KEY), ("authorization", "Bearer token")]);
        assert_eq!(outcome(&snap, &two, 1003, false), "ask");
    }

    #[test]
    fn a_refusal_is_remembered_for_a_while_and_only_under_its_generation() {
        let (mut denied, digest, t0) = (Denied::default(), hash_key(b"Ksk-other"), Instant::now());
        assert!(!denied.holds(&digest, 7, t0));
        denied.learn(digest, 7, t0);
        assert!(denied.holds(&digest, 7, t0 + Duration::from_secs(9)));
        assert!(!denied.holds(&digest, 7, t0 + DENIED_FOR));
        assert!(!denied.holds(&digest, 8, t0), "the snapshot changed, or the link broke: ask again");
        assert!(!denied.holds(&hash_key(b"Ask-other"), 7, t0));
        for i in 0..DENIED_MAX as u32 {
            denied.learn(hash_key(&i.to_le_bytes()), 7, t0);
        }
        assert!(denied.0.len() <= DENIED_MAX && !denied.holds(&digest, 7, t0), "full: everything is forgotten");
    }

    #[test]
    fn the_401_is_the_python_envelope() {
        let snap = governed(REFUSALS, "");
        let req = with_headers(&[]);
        let Decision::Refuse(u) = decide(&snap, peer(), &req, 0, |_| false) else { panic!("refused") };
        let t = Trace { request_id: "r1".into(), traceparent: String::new() };
        let resp = envelope(StatusCode::UNAUTHORIZED, "AUTHENTICATION_ERROR", &u.wording.message_json, &u.wording.details_json, &t, true);
        assert_eq!(resp.status(), 401);
        let v = body_of(resp);
        assert_eq!((v["error"].as_str(), v["message"].as_str(), &v["details"]), (Some("AUTHENTICATION_ERROR"), Some("API key required"), &serde_json::json!({"hint":"h1"})));
        assert_eq!(u.version, Some("1"));
    }

    #[test]
    fn bearer_is_taken_out_wherever_it_stands() {
        for (value, key) in [("Bearer abc", "abc"), ("abc", "abc"), ("Bearer Bearer abc", "abc"), ("aBearer bc", "abc"), ("Bearer", "Bearer"), ("BearerBearer  x", "Bearer x"), ("bearer abc", "bearer abc")] {
            assert_eq!(&*without_bearer(value.as_bytes()), key.as_bytes(), "{value}");
        }
    }

    fn probe(snap: &Snapshot, method: Method, uri: &str, headers: &[(&str, &str)], up: bool, ready: Option<u16>) -> Option<String> {
        let mut req = Request::builder().method(method).uri(uri);
        for (name, value) in headers {
            req = req.header(*name, *value);
        }
        let req = req.body(()).unwrap();
        let ready = ready.map(|status| Arc::new(Readiness { status, body_json: "{}".into() }));
        own(snap, &req, || up, || ready).map(|o| match o {
            Own::Health(v) => format!("health {v}"),
            Own::Ready(r) => format!("ready {}", r.status),
            Own::Live => "live".into(),
        })
    }

    #[test]
    fn the_gateways_own_endpoints_are_answered_here_when_this_side_can_speak_for_it() {
        let snap = Snapshot::from_json(r#"{"keys":[],"ops":{"health_path":"/health","version_json":"\"1.2\""}}"#).unwrap();
        let get = |uri: &str, up: bool, ready: Option<u16>| probe(&snap, Method::GET, uri, &[], up, ready);
        assert_eq!(get("/health", true, None).as_deref(), Some("health \"1.2\""));
        assert_eq!(get("/health?verbose=1", true, None).as_deref(), Some("health \"1.2\""));
        assert_eq!(get("/ready", true, Some(200)).as_deref(), Some("ready 200"));
        assert_eq!(get("/ready", true, Some(503)).as_deref(), Some("ready 503"));
        // Health and readiness speak for the Python gateway: not without its word.
        assert_eq!(get("/health", false, None), None);
        assert_eq!(get("/ready", true, None), None);
        // Being alive is this process's own business.
        assert_eq!(get("/live", false, None).as_deref(), Some("live"));
        // Only these paths, exactly.
        for uri in ["/health/", "/health/detailed", "/healthz", "/ready/", "/live/x", "/metrics", "/api/health", "/"] {
            assert_eq!(get(uri, true, Some(200)), None, "{uri}");
        }
        // Anything but a plain GET is the Python gateway's to answer (405, a preflight, a handshake).
        for method in [Method::HEAD, Method::POST, Method::OPTIONS] {
            assert_eq!(probe(&snap, method, "/live", &[], true, None), None);
        }
        assert_eq!(probe(&snap, Method::GET, "/live", &[("Origin", "https://app.example")], true, None), None);
        assert_eq!(probe(&snap, Method::GET, "/health", &[("Upgrade", "websocket")], true, None), None);
        assert_eq!(probe(&snap, Method::GET, "http://other.example/live", &[], true, None), None);
    }

    #[test]
    fn the_health_path_is_wherever_the_gateway_has_it_and_comes_first() {
        let moved = Snapshot::from_json(r#"{"keys":[],"ops":{"health_path":"/api/status","version_json":"\"1\""}}"#).unwrap();
        assert_eq!(probe(&moved, Method::GET, "/api/status", &[], true, None).as_deref(), Some("health \"1\""));
        assert_eq!(probe(&moved, Method::GET, "/health", &[], true, None), None);
        // Configured onto another endpoint's path, the health route wins there, as it does in Python.
        let shadowing = Snapshot::from_json(r#"{"keys":[],"ops":{"health_path":"/live","version_json":"\"1\""}}"#).unwrap();
        assert_eq!(probe(&shadowing, Method::GET, "/live", &[], true, None).as_deref(), Some("health \"1\""));
        assert_eq!(probe(&shadowing, Method::GET, "/live", &[], false, None), None);
        // No path, or no word from the control plane at all: health is Python's, alive is still ours.
        let hidden = Snapshot::from_json(r#"{"keys":[],"ops":{"health_path":null,"version_json":"\"1\""}}"#).unwrap();
        for snap in [&hidden, &Snapshot::empty()] {
            assert_eq!(probe(snap, Method::GET, "/health", &[], true, None), None);
            assert_eq!(probe(snap, Method::GET, "/live", &[], true, None).as_deref(), Some("live"));
        }
    }

    fn handshake(uri: &str, headers: &[(&str, &str)]) -> Request<()> {
        let mut b = Request::builder().uri(uri);
        for (name, value) in [("upgrade", "websocket"), ("connection", "Upgrade"), ("sec-websocket-version", "13"), ("sec-websocket-key", "a2V5")] {
            if !headers.iter().any(|(n, _)| n.eq_ignore_ascii_case(name)) {
                b = b.header(name, value);
            }
        }
        for (name, value) in headers.iter().filter(|(_, v)| !v.is_empty()) {
            b = b.header(*name, *value);
        }
        b.body(()).unwrap()
    }

    const WS: &str = r#","websocket":"ws://127.0.0.1:7000/base/""#;

    #[test]
    fn a_websocket_handshake_with_a_working_key_is_relayed_here() {
        let snap = governed(WS, "");
        let target = |uri: &str, headers: &[(&str, &str)]| relay(&snap, peer(), &handshake(uri, headers), Duration::from_secs(100)).map(|r| r.target);
        let key = [("x-api-key", KEY)];
        assert_eq!(target("/ws/chat", &key).as_deref(), Some("/base/chat"));
        assert_eq!(target("/ws/rooms/7?room=blue&x=1", &key).as_deref(), Some("/base/rooms/7?room=blue&x=1"));
        assert_eq!(target("/ws/", &key).as_deref(), Some("/base/"));
        assert_eq!(target("/ws/chat", &[("x-api-key", KEY), ("upgrade", "WebSocket"), ("authorization", "Bearer t")]).as_deref(), Some("/base/chat"));
        // Whatever is not certain to be let in is the Python gateway's to judge.
        for (why, uri, headers) in [
            ("no key", "/ws/chat", vec![]),
            ("unknown key", "/ws/chat", vec![("x-api-key", "sk-nope")]),
            ("an unknown key in the query", "/ws/chat?api_key=sk-nope", key.to_vec()),
            ("an escaped key in the query", "/ws/chat?api_key=sk%2Dtest", key.to_vec()),
            ("a key with a plus in the query", "/ws/chat?api_key=sk+test", key.to_vec()),
            ("an escaped name in the query", "/ws/chat?api%5Fkey=sk-test", key.to_vec()),
            ("key as a Bearer token", "/ws/chat", vec![("authorization", "Bearer sk-test")]),
            ("not under /ws/", "/ws", key.to_vec()),
            ("not under /ws/", "/wsx/chat", key.to_vec()),
            ("an agent's socket", "/agents/a/ws/chat", key.to_vec()),
            ("an escaped path", "/ws/a%2Fb", key.to_vec()),
            ("not a WebSocket", "/ws/chat", vec![("x-api-key", KEY), ("upgrade", "h2c")]),
            ("another version", "/ws/chat", vec![("x-api-key", KEY), ("sec-websocket-version", "8")]),
            ("no handshake key", "/ws/chat", vec![("x-api-key", KEY), ("sec-websocket-key", "")]),
        ] {
            assert!(target(uri, &headers).is_none(), "{why}");
        }
        // The key may be in the query, where the last one given stands before the header's;
        // the backend is sent the query without it and without empty parts.
        assert_eq!(target(&format!("/ws/chat?api_key={KEY}"), &[]).as_deref(), Some("/base/chat"));
        assert_eq!(target(&format!("/ws/chat?api_key={KEY}"), &[("x-api-key", "sk-nope")]).as_deref(), Some("/base/chat"));
        assert_eq!(target(&format!("/ws/chat?api_key=sk-nope&a=1&&api_key={KEY}&b"), &[]).as_deref(), Some("/base/chat?a=1&b"));
        assert_eq!(target("/ws/chat?api_key=&a=1&", &key).as_deref(), Some("/base/chat?a=1"));
        assert_eq!(target("/ws/chat?api_key", &key).as_deref(), Some("/base/chat"));
        assert_eq!(target("/ws/chat?api_keys=x&xapi_key=y", &key).as_deref(), Some("/base/chat?api_keys=x&xapi_key=y"));
        assert!(target(&format!("/ws/chat?api_key={KEY}&api_key=sk-nope"), &key).is_none());
        let twice = Request::builder().uri("/ws/chat").header("upgrade", "websocket").header("sec-websocket-version", "13")
            .header("sec-websocket-key", "a2V5").header("x-api-key", KEY).header("x-api-key", "sk-other").body(()).unwrap();
        assert!(relay(&snap, peer(), &twice, Duration::from_secs(100)).is_none());
        let post = Request::builder().method("POST").uri("/ws/chat").header("upgrade", "websocket").header("sec-websocket-version", "13")
            .header("sec-websocket-key", "a2V5").header("x-api-key", KEY).body(()).unwrap();
        assert!(relay(&snap, peer(), &post, Duration::from_secs(100)).is_none());
    }

    #[test]
    fn a_websocket_is_under_the_ip_rules_and_the_policies_as_a_get_for_its_path() {
        let req = handshake("/ws/chat", &[("x-api-key", KEY)]);
        let let_in = |extra: &str, req: &Request<()>, at: u64| relay(&governed(&format!("{WS}{extra}"), ""), peer(), req, Duration::from_secs(at)).is_some();
        let rule = |scope: &str, id: &str, allow: bool, net: &str| format!(r#","ip_rules":[{{"scope":"{scope}","scope_id":{id},"allow":{allow},"net":"{net}","details_json":"{{}}"}}]"#);
        // The caller's address is the peer's; a rule that blocks it leaves the handshake to Python.
        assert!(let_in(&rule("global", "null", false, "192.0.2.0/24"), &req, 100));
        assert!(!let_in(&rule("global", "null", false, "203.0.113.0/24"), &req, 100));
        assert!(!let_in(&rule("global", "null", true, "192.0.2.0/24"), &req, 100), "an allow list the caller is not on");
        assert!(!let_in(&rule("api_key", "\"k1\"", false, "203.0.113.0/24"), &req, 100));
        assert!(let_in(&rule("api_key", "\"k9\"", false, "203.0.113.0/24"), &req, 100), "another key's rule");
        let policy = |path: &str, methods: &str, time: &str, deny: bool| {
            format!(r#","policies":[{{"path":{path},"methods":[{methods}],"tenants":[],"cidrs":null,"time":{time},"deny":{deny},"message_json":"\"no\"","details_json":"{{}}"}}]"#)
        };
        assert!(!let_in(&policy("\"/ws/*\"", "", "null", true), &req, 100));
        assert!(let_in(&policy("\"/api/*\"", "", "null", true), &req, 100), "a policy for other paths");
        assert!(!let_in(&policy("null", "\"GET\"", "null", true), &req, 100), "a handshake is a GET");
        assert!(let_in(&policy("null", "\"POST\"", "null", true), &req, 100));
        assert!(let_in(&policy("\"/ws/*\"", "", "null", false), &req, 100), "a policy that allows");
        // A daily window, in UTC: 01:00 to 02:00.
        assert!(!let_in(&policy("null", "", "[60,120]", true), &req, 3_700));
        assert!(let_in(&policy("null", "", "[60,120]", true), &req, 100));
        // With rules in force, an address this side cannot be sure of is Python's to read.
        let proxied = r#","trusted_proxies":["203.0.113.7/32"]"#;
        let blocked = rule("global", "null", false, "198.51.100.0/24");
        let via = |forwarded: &[&str]| {
            let mut b = Request::builder().uri("/ws/chat").header("upgrade", "websocket").header("sec-websocket-version", "13")
                .header("sec-websocket-key", "a2V5").header("x-api-key", KEY);
            for f in forwarded {
                b = b.header("x-forwarded-for", *f);
            }
            b.body(()).unwrap()
        };
        assert!(let_in(&format!("{proxied}{blocked}"), &via(&["192.0.2.9"]), 100));
        assert!(!let_in(&format!("{proxied}{blocked}"), &via(&["198.51.100.7"]), 100), "the forwarded address is the caller's");
        assert!(!let_in(&format!("{proxied}{blocked}"), &via(&["192.0.2.9", "192.0.2.10"]), 100), "given twice");
        assert!(let_in(proxied, &via(&["192.0.2.9", "192.0.2.10"]), 100), "no rule in force: the address is not needed");
    }

    #[test]
    fn an_agents_websocket_is_relayed_here_without_a_key() {
        let snap = governed(AGENTS, "");
        let target = |uri: &str, headers: &[(&str, &str)]| agent_relay(&snap, &handshake(uri, headers)).map(|r| (r.backend.upstream.authority.to_string(), r.target, r.entry.is_some()));
        let to = |authority: &str, target: &str| Some((authority.to_owned(), target.to_owned(), false));
        assert_eq!(target("/agents/hiring/ws/chat", &[]), to("10.0.0.10:8001", "/ws/chat"));
        assert_eq!(target("/agents/hiring", &[]), to("10.0.0.10:8001", "/"));
        assert_eq!(target("/agents/hiring/", &[("x-api-key", "sk-nope")]), to("10.0.0.10:8001", "/"));
        assert_eq!(target("/agents/hiring/a?", &[]), to("10.0.0.10:8001", "/a"));
        // The query goes as it came: the agent may well read a key of its own there.
        assert_eq!(target("/agents/based/a?token=t&api_key=x&&q=a+b%20c", &[]), to("10.0.0.11:8001", "/base/a?token=t&api_key=x&&q=a+b%20c"));
        for (why, uri, headers) in [
            ("an unknown slug", "/agents/nobody/a", vec![]),
            ("no slug", "/agents/", vec![]),
            ("behind TLS, without a file of certificates", "/agents/secure/a", vec![]),
            ("an escaped path", "/agents/hiring/a%20b", vec![]),
            ("not under /agents/", "/api/agents/hiring/a", vec![]),
            ("not a WebSocket", "/agents/hiring/a", vec![("upgrade", "h2c")]),
            ("another version", "/agents/hiring/a", vec![("sec-websocket-version", "8")]),
        ] {
            assert!(target(uri, &headers).is_none(), "{why}");
        }
        assert!(agent_relay(&governed("", ""), &handshake("/agents/hiring/a", &[])).is_none());
    }

    #[test]
    fn a_websocket_is_relayed_here_only_with_a_backend_and_a_key_that_may() {
        let req = handshake("/ws/chat", &[("x-api-key", KEY)]);
        // No WebSocket backend this side can reach.
        assert!(relay(&governed("", ""), peer(), &req, Duration::from_secs(100)).is_none());
        // Behind TLS only where this side verifies against a file of certificates it was given.
        assert!(relay(&governed(r#","websocket":"wss://secure.example/base""#, ""), peer(), &req, Duration::from_secs(100)).is_none());
        assert!(relay(&governed(r#","websocket":"http://127.0.0.1:7000""#, ""), peer(), &req, Duration::from_secs(100)).is_none());
        // A key that is off, not reproducible here, expired, or not allowed this path.
        for key_extra in [r#","is_active":false"#, r#","native_ok":false"#, r#","expires_at":100"#, r#","expires_at":99"#] {
            assert!(relay(&governed(WS, key_extra), peer(), &req, Duration::from_secs(100)).is_none(), "{key_extra}");
        }
        assert!(relay(&governed(WS, r#","expires_at":101"#), peer(), &req, Duration::from_secs(100)).is_some());
        let scoped = governed(WS, r#","allowed_paths":{"rules":[["under","/ws/chat"]],"patterns_json":"[\"/ws/chat\"]"}"#);
        assert!(relay(&scoped, peer(), &req, Duration::from_secs(100)).is_some());
        assert!(relay(&scoped, peer(), &handshake("/ws/other", &[("x-api-key", KEY)]), Duration::from_secs(100)).is_none());
    }

    #[test]
    fn a_request_a_transformation_rule_applies_to_is_left_to_python() {
        let rules = r#","transform_rules":[{"glob":"/api/orders/*","methods":["GET","POST"]},{"glob":"/api/item?","methods":[]},
            {"glob":"api/answers/*","methods":[]},{"glob":"/shaped/*","methods":[]},{"glob":"/pkg.Service/*","methods":[]}]"#;
        let snap = governed(&format!("{AGENTS}{GRPC}{rules}"), "");
        let native = |method: &str, uri: &str| {
            let req = Request::builder().method(method).uri(uri).header("x-api-key", KEY).body(()).unwrap();
            outcome(&snap, &req, 100, false)
        };
        assert_eq!(native("GET", "/api/users"), "native");
        assert_eq!(native("GET", "/api/orders/7"), "ask");
        assert_eq!(native("POST", "/api/orders/7/lines"), "ask");
        // Another method, and a path the pattern does not cover in full.
        assert_eq!(native("DELETE", "/api/orders/7"), "native");
        assert_eq!(native("GET", "/api/orders"), "native");
        assert_eq!(native("PUT", "/api/item7"), "ask");
        assert_eq!(native("PUT", "/api/item77"), "native");
        // A rule for the answer is matched by the path without its leading slash.
        assert_eq!(native("GET", "/api/answers/1"), "ask");
        // An agent is asked for the path after its slug, and that is what a rule sees.
        assert_eq!(native("GET", "/agents/hiring/shaped/a"), "ask");
        assert_eq!(native("GET", "/agents/hiring/other/a"), "agent");
        // A gRPC call has nobody to be left to.
        let call = rpc("/pkg.Service/Method", &[("x-api-key", KEY)]);
        assert_eq!(admit_grpc(&snap, peer(), &call, 100, true).err(), Some(GrpcRefusal::NOT_APPLICABLE));
        assert!(admit_grpc(&snap, peer(), &rpc("/pkg.Other/Method", &[("x-api-key", KEY)]), 100, true).is_ok());
        // Rules the control plane could not describe: every request is taken to be one of theirs.
        let unknown = governed(&format!(r#"{AGENTS}{GRPC},"transform_rules":null"#), "");
        let req = Request::builder().uri("/api/users").header("x-api-key", KEY).body(()).unwrap();
        assert_eq!(outcome(&unknown, &req, 100, false), "ask");
        assert_eq!(admit_grpc(&unknown, peer(), &call, 100, true).err(), Some(GrpcRefusal::NOT_APPLICABLE));
        // A control plane that says nothing about rules has none.
        assert_eq!(outcome(&governed("", ""), &req, 100, false), "native");
    }

    #[test]
    fn a_rule_this_side_was_told_the_actions_of_is_applied_here() {
        let rules = r#","transform_rules":[
            {"glob":"/api/shaped/*","methods":[],"native":true,"response":null,"request":{"rename":[["X-Old","X-New"]],"remove":["x-secret"],
                "set":[["X-Shaped","yes"],["x-client","gateway"]],"query_remove":["debug"],"query_set":[["source","gw"]]}},
            {"glob":"*shaped/*","methods":["POST"],"native":true,"request":{"rename":[["x-shaped","X-Was"]],"remove":[],"set":[]},
                "response":{"rename":[["X-Agent","X-Backend"]],"remove":["etag"],"set":[["X-Powered","gateway"]],"status":[[200,201],[418,400]]}},
            {"glob":"*shaped/twice/*","methods":[],"native":true,"request":null,"response":{"status":[[201,202]]}},
            {"glob":"/api/kept/*","methods":[],"native":false},
            {"glob":"/api/nothing/*","methods":[],"native":true,"request":null,"response":null},
            {"glob":"api/talk/*","methods":[],"native":true,"request":null,"response":{"set":[["X-Powered","gateway"]]}},
            {"glob":"*","methods":["PUT"],"native":true,"request":{"set":[["X-Shaped","yes"]]},"response":null}]"#;
        let snap = governed(&format!("{GRPC}{rules}"), "");
        let ask = |method: &str, uri: &str, headers: &[(&str, &str)]| {
            let mut req = Request::builder().method(method).uri(uri).header("x-api-key", KEY);
            for (name, value) in headers {
                req = req.header(*name, *value);
            }
            req.body(()).unwrap()
        };
        let shaped = |req: &Request<()>| match decide(&snap, peer(), req, 100, |_| false) {
            Decision::Native(n) => {
                let mut sent = Request::builder().uri(req.uri().clone()).body(full(Bytes::new())).unwrap();
                *sent.headers_mut() = req.headers().clone();
                shape_request(&mut sent, &n.shaping.request, false, None);
                let mut resp = Response::new(full(Bytes::new()));
                for (name, value) in [("x-agent", "echo"), ("etag", "1"), ("etag", "2"), ("set-cookie", "a=1"), ("set-cookie", "b=2")] {
                    resp.headers_mut().append(name, HeaderValue::from_static(value));
                }
                shape_answer(&mut resp, &n.shaping.response);
                let read = |h: &HeaderMap, name: &str| h.get_all(name).iter().map(|v| v.to_str().unwrap()).collect::<Vec<_>>().join(", ");
                let mut names: Vec<&str> = sent.headers().keys().chain(resp.headers().keys()).map(|k| k.as_str()).collect();
                names.sort_unstable();
                names.dedup();
                let said: Vec<String> = names.iter().filter(|n| **n != "x-api-key").map(|n| format!("{n}={}|{}", read(sent.headers(), n), read(resp.headers(), n))).collect();
                format!("{} {} {}", sent.uri(), resp.status().as_u16(), said.join(" "))
            }
            _ => "ask".to_owned(),
        };
        // Renamed, removed, set; a name set in another case stands beside the caller's own.
        let req = ask("GET", "/api/shaped/1?debug=1&page=2", &[("x-old", "o"), ("x-secret", "s"), ("x-client", "mine"), ("x-shaped", "no"), ("x-keep", "k")]);
        assert_eq!(shaped(&req), "/api/shaped/1?page=2&source=gw 200 etag=|1, 2 set-cookie=|a=1, b=2 x-agent=|echo x-client=gateway| x-keep=k| x-new=o| x-shaped=no, yes|");
        // The second rule follows the first, for its method, and changes the answer too: a
        // header it names that the backend gave twice is one line; another is left as it came.
        let req = ask("POST", "/api/shaped/1", &[]);
        assert_eq!(shaped(&req), "/api/shaped/1?source=gw 201 set-cookie=|a=1, b=2 x-backend=|echo x-client=gateway| x-powered=|gateway x-was=yes|");
        assert_eq!(shaped(&ask("POST", "/api/shaped/twice/1", &[])).split(' ').nth(1), Some("202"));
        assert_eq!(shaped(&ask("GET", "/api/shaped/twice/1", &[])).split(' ').nth(1), Some("200"));
        // No rule, a rule that does nothing, and a rule of the Python gateway's.
        assert_eq!(shaped(&ask("GET", "/api/users?a=b%20c", &[])), "/api/users?a=b%20c 200 etag=|1, 2 set-cookie=|a=1, b=2 x-agent=|echo");
        assert_eq!(shaped(&ask("GET", "/api/nothing/1", &[])), "/api/nothing/1 200 etag=|1, 2 set-cookie=|a=1, b=2 x-agent=|echo");
        assert_eq!(shaped(&ask("GET", "/api/kept/1", &[])), "ask");
        // Of a header given twice a rule reads the first line, and one no rule changed goes on
        // with every line; a query is written again as the Python gateway writes it, a name
        // given twice with every value unless a rule changed it.
        let once = |req: &Request<()>| shaped(req).split(' ').filter(|part| part.starts_with('/') || part.starts_with("x-new") || part.starts_with("x-keep")).collect::<Vec<_>>().join(" ");
        assert_eq!(once(&ask("GET", "/api/shaped/1", &[("x-old", "a"), ("x-old", "b")])), "/api/shaped/1?source=gw x-new=a|");
        assert!(shaped(&ask("GET", "/api/shaped/1", &[("x-keep", "a"), ("x-keep", "b")])).contains(" x-keep=a, b| "));
        assert!(shaped(&ask("GET", "/api/users", &[("x-keep", "a"), ("x-keep", "b")])).ends_with(" x-keep=a, b|"));
        assert_eq!(shaped(&ask("GET", "/api/users", &[("te", "trailers"), ("te", "gzip")])), "ask");
        assert_eq!(once(&ask("GET", "/api/shaped/1?q=a+b&debug=1", &[])), "/api/shaped/1?q=a%20b&source=gw");
        assert_eq!(once(&ask("GET", "/api/shaped/1?q=1&q=2", &[])), "/api/shaped/1?q=1&q=2&source=gw");
        // The rule sets `source`: set to another value it is the rule's, to the value held it is as it came.
        assert_eq!(once(&ask("GET", "/api/shaped/1?page=1&page=2&source=gw&source=x", &[])), "/api/shaped/1?page=1&page=2&source=gw");
        assert_eq!(once(&ask("GET", "/api/shaped/1?source=x&source=gw", &[])), "/api/shaped/1?source=x&source=gw");
        assert_eq!(once(&ask("PUT", "/api/users?q=a+b&flag", &[])), "/api/users?q=a%20b&flag=");
        assert_eq!(once(&ask("GET", "/api/users?", &[])), "/api/users");
        // A query whose escapes are not text is read by the Python gateway alone.
        assert_eq!(shaped(&ask("GET", "/api/users?q=%ff", &[])), "ask");
        assert_eq!(shaped(&ask("GET", "/api/shaped/1?q=%ff", &[])), "ask");
        // An answer the Python gateway may stream is passed on there with no rule applied.
        assert!(shaped(&ask("GET", "/api/talk/1", &[])).contains("x-powered=|gateway"));
        let streamed = shaped(&ask("GET", "/api/talk/message:stream", &[]));
        assert!(streamed != "ask" && !streamed.contains("x-powered"), "{streamed}");
        assert!(shaped(&ask("GET", "/api/talk/Message%3AStream", &[])).contains("x-powered=|gateway"));
        assert_ne!(shaped(&ask("POST", "/api/shaped/acp/runs", &[])), "ask");
        assert_eq!(shaped(&ask("POST", "/api/shaped/acp/runs?stream=%31", &[])), "ask");
        assert_ne!(shaped(&ask("GET", "/api/shaped/acp/runs", &[])), "ask");
        // gRPC and publishing have handlers of their own there, which apply no rule.
        assert_eq!(shaped(&ask("PUT", "/grpc/x", &[])), "ask");
    }

    #[test]
    fn every_form_of_pattern_is_matched_as_the_python_gateway_matches_it() {
        let with = |rule: &str| governed(&format!(r#","transform_rules":[{rule}]"#), "");
        let asks = |snap: &Snapshot, method: &str, uri: &str| {
            let req = Request::builder().method(method).uri(uri).header("x-api-key", KEY).body(()).unwrap();
            outcome(snap, &req, 100, false) == "ask"
        };
        // Every path, for the rule's methods only.
        let all = with(r#"{"all":true,"methods":["POST"]}"#);
        assert!(asks(&all, "POST", "/api/users") && !asks(&all, "GET", "/api/users"));
        // Character classes: the characters each lets through, given by the Python gateway.
        let classes = with(r#"{"parts":[{"lit":"/api/v"},{"set":"12"},{"lit":"/"},"*",{"lit":"/item"},"?"],"methods":[]}"#);
        assert!(asks(&classes, "GET", "/api/v1/a/b/item7") && asks(&classes, "GET", "/api/v2//itemx"));
        assert!(!asks(&classes, "GET", "/api/v3/a/item7") && !asks(&classes, "GET", "/api/v1/a/item") && !asks(&classes, "GET", "/api/v1/a/item77"));
        // A regular expression matches from the start of the path, and need not reach its end.
        let regex = with(r#"{"regex":"^/api/v[0-9]+/(?:orders|items)","methods":[]}"#);
        assert!(asks(&regex, "GET", "/api/v12/orders/7") && asks(&regex, "GET", "/api/v1/itemsets"));
        assert!(!asks(&regex, "GET", "/api/v/orders") && !asks(&regex, "GET", "/api/x/api/v1/orders"));
        let ended = with(r#"{"regex":"/api/.*\\.json$","methods":[]}"#);
        assert!(asks(&ended, "GET", "/api/a/b.json") && !asks(&ended, "GET", "/api/a/b.json5"));
        assert!(!asks(&with(r#"{"regex":"/users/.*$","methods":[]}"#), "GET", "/api/users/1"));
        // For the answer, the path is matched without its leading slash.
        assert!(asks(&with(r#"{"regex":"api/users$","methods":[]}"#), "GET", "/api/users"));
        // What is not a regular expression after all is text the path must contain.
        let contains = with(r#"{"contains":"/(x","methods":[]}"#);
        assert!(asks(&contains, "GET", "/api/(x/1") && !asks(&contains, "GET", "/api/x/1"));
        // A form this side does not know, two forms at once, or an expression it cannot compile:
        // no rule can be trusted, and every request is the Python gateway's.
        for odd in [r#"{"methods":[]}"#, r#"{"all":true,"glob":"/x","methods":[]}"#, r#"{"regex":"^/api/(?=x)","methods":[]}"#,
                    r#"{"parts":["**"],"methods":[]}"#, r#"{"parts":[{"set":"\u00e9"}],"methods":[]}"#] {
            assert!(asks(&with(odd), "GET", "/api/users"), "{odd}");
        }
    }

    #[test]
    fn globs_match_as_fnmatch_matches_them() {
        use crate::snapshot::glob_matches as m;
        for (pattern, text, want) in [
            ("/api/users/*", "/api/users/7", true), ("/api/users/*", "/api/users/", true), ("/api/users/*", "/api/users", false),
            ("/api/*/lines", "/api/orders/7/lines", true), ("/api/*/lines", "/api/orders/7/line", false),
            ("*.json", "/a/b.json", true), ("*.json", "/a/b.jsonl", false), ("/a?c", "/abc", true), ("/a?c", "/ac", false),
            ("/a?c", "/a/c", true), ("", "", true), ("", "/", false), ("***", "", true), ("/a*b*c", "/aXbXbYc", true),
            ("/a*b*c", "/aXbXbY", false), ("/A", "/a", false), ("/a.b", "/aXb", false),
        ] {
            assert_eq!(m(pattern.as_bytes(), text.as_bytes()), want, "{pattern} against {text}");
        }
    }

    const ROUTES: &str = r#","dynamic_routes":[
        {"path":"/api/exact","wildcard":false,"backend":"http://10.0.1.1:9000","protocol":"http","timeout_seconds":30,"rewrite":null},
        {"path":"/api/orders/new/","wildcard":true,"backend":"http://10.0.1.2:9000","protocol":"mcp","timeout_seconds":5,"rewrite":"/intake"},
        {"path":"/api/orders/","wildcard":true,"backend":"http://10.0.1.3:9000/base","protocol":"http","timeout_seconds":30,"rewrite":null},
        {"path":"/api/secure/","wildcard":true,"backend":null,"protocol":"http","timeout_seconds":null,"rewrite":null},
        {"path":"/api/versioned/","wildcard":true,"backend":"http://10.0.1.4:9000","protocol":"http","timeout_seconds":30,"rewrite":"/v2/x"},
        {"path":"/acp/routed/","wildcard":true,"backend":"http://10.0.1.5:9000","protocol":"acp","timeout_seconds":30,"rewrite":null}]"#;

    #[test]
    fn the_first_dynamic_route_that_matches_decides_where_a_request_goes() {
        let snap = governed(ROUTES, "");
        let routed = |uri: &str| {
            let req = Request::builder().uri(uri).header("x-api-key", KEY).body(()).unwrap();
            route(&snap, peer(), &req, 100).map(|n| (n.backend.map(|b| b.upstream.text.clone()), n.route.map(|r| (r.protocol.clone(), r.rewrite.clone(), r.timeout()))))
        };
        let to = |url: &str, protocol: &str, rewrite: Option<&str>, secs: u64| {
            Some((Some(url.to_owned()), Some((protocol.to_owned(), rewrite.map(str::to_owned), Some(Duration::from_secs(secs))))))
        };
        assert_eq!(routed("/api/exact"), to("http://10.0.1.1:9000", "http", None, 30));
        // An exact path is that path only; a wildcard is everything under its prefix, the longest first.
        assert_eq!(routed("/api/exact/more"), Some((None, None)));
        assert_eq!(routed("/api/orders/new/7"), to("http://10.0.1.2:9000", "mcp", Some("/intake"), 5));
        assert_eq!(routed("/api/orders/7?x=1"), to("http://10.0.1.3:9000/base", "http", None, 30));
        assert_eq!(routed("/api/orders"), Some((None, None)));
        assert_eq!(routed("/api/users"), Some((None, None)));
        // A route this side cannot follow.
        assert_eq!(routed("/api/secure/x"), None);
        // One that leads to a path naming an API version, which the backend is then told.
        assert_eq!(routed("/api/versioned/x"), to("http://10.0.1.4:9000", "http", Some("/v2/x"), 30));
        assert_eq!(route(&snap, peer(), &request("GET", "/api/versioned/x"), 0).map(|n| (n.version, n.told)), Some((Some("1"), Some("2"))));
        // A protocol's path that is routed needs no backend of the protocol's own.
        assert_eq!(routed("/acp/routed/runs"), to("http://10.0.1.5:9000", "acp", None, 30));
        assert_eq!(routed("/acp/runs"), None);
    }

    #[test]
    fn the_gateways_own_request_headers_are_written_as_the_python_gateway_writes_them() {
        let trace = Trace { request_id: "id-1".to_owned(), traceparent: "tp".to_owned() };
        let sent = Sent { trace: &trace, version: Some("2"), notices: None, inject_request_id: true, protocol: "http", path: None };
        let read = |headers: &HeaderMap, name: &str| headers.get_all(name).iter().map(|v| v.to_str().unwrap().to_owned()).collect::<Vec<_>>();
        let came = || {
            let mut headers = HeaderMap::new();
            for (name, value) in [("x-forwarded-for", "198.51.100.7"), ("x-protocol", "mine"), ("x-api-version", "2"), ("traceparent", "theirs"), ("x-gateway-note", "n")] {
                headers.append(name, HeaderValue::from_static(value));
            }
            headers
        };
        // With no rule: the gateway's spellings are other names than the caller's, whose lines go
        // on before them; `traceparent` is the caller's own spelling, and is replaced.
        let mut headers = came();
        stamp_request(&mut headers, &sent);
        assert_eq!(read(&headers, "x-forwarded-for"), ["198.51.100.7", ""]);
        assert_eq!(read(&headers, "x-protocol"), ["mine", "http"]);
        assert_eq!(read(&headers, "x-api-version"), ["2", "2"]);
        assert_eq!(read(&headers, "traceparent"), ["tp"]);
        assert_eq!(read(&headers, "x-request-id"), ["id-1"]);
        assert!(read(&headers, "x-gateway-note").is_empty());
        let mut bare = HeaderMap::new();
        stamp_request(&mut bare, &Sent { version: None, inject_request_id: false, ..Sent { trace: &trace, version: None, notices: None, inject_request_id: false, protocol: "mcp", path: None } });
        assert_eq!((read(&bare, "x-forwarded-for"), read(&bare, "x-protocol"), read(&bare, "traceparent")), (vec![String::new()], vec!["mcp".to_owned()], vec!["tp".to_owned()]));
        assert!(read(&bare, "x-request-id").is_empty() && read(&bare, "x-api-version").is_empty());

        // With rules: a line left under the gateway's own spelling is the one the gateway writes
        // to (or, for X-Forwarded-For, leaves); one under any other spelling stays beside it.
        let rule = |text: &str| serde_json::from_str::<Actions>(text).unwrap();
        let named = rule(r#"{"rename":[["x-request-id","X-Caller-Request"]],"remove":[],"set":[["X-Forwarded-For","203.0.113.5"],["x-protocol","theirs"],["Traceparent","nope"]]}"#);
        let mut headers = came();
        assert!(shape_headers(&mut headers, &[&named], Some(&sent), &[]));
        assert_eq!(read(&headers, "x-forwarded-for"), ["198.51.100.7", "203.0.113.5"]);
        assert_eq!(read(&headers, "x-protocol"), ["theirs", "http"]);
        assert_eq!(read(&headers, "traceparent"), ["tp", "nope"]);
        // The id the plugin added before the rules is the one renamed.
        assert_eq!((read(&headers, "x-request-id"), read(&headers, "x-caller-request")), (vec![], vec!["id-1".to_owned()]));
        assert!(read(&headers, "x-gateway-note").is_empty());
        let exact = rule(r#"{"remove":["x-forwarded-for","traceparent","x-request-id"],"set":[["X-Protocol","theirs"],["X-Request-ID","mine"]]}"#);
        let mut headers = came();
        assert!(shape_headers(&mut headers, &[&exact], Some(&sent), &[]));
        assert_eq!((read(&headers, "x-forwarded-for"), read(&headers, "traceparent")), (vec![String::new()], vec!["tp".to_owned()]));
        // The caller's own `x-protocol` stays; the rule's line is under the gateway's spelling and is written over.
        assert_eq!(read(&headers, "x-protocol"), ["mine", "http"]);
        assert_eq!(read(&headers, "x-request-id"), ["mine"]);
        // An answer's headers, and rules that change no header, are not the gateway's to write here.
        let mut headers = came();
        assert!(!shape_headers(&mut headers, &[&named], None, &[]));
        assert_eq!((read(&headers, "x-protocol"), read(&headers, "x-forwarded-for")), (vec!["theirs".to_owned()], vec!["198.51.100.7".to_owned(), "203.0.113.5".to_owned()]));
        assert!(!shape_headers(&mut came(), &[&rule(r#"{"query_set":[["a","1"]]}"#)], Some(&sent), &[]));
    }

    #[test]
    fn a_route_to_a_backend_that_is_not_http_goes_where_the_python_gateways_client_takes_it() {
        let snap = governed(
            r#","dynamic_routes":[
            {"path":"/api/sockets/","wildcard":true,"backend":"ws://10.0.2.1:9000/base","protocol":"http","timeout_seconds":30,"rewrite":null},
            {"path":"/api/plain/","wildcard":true,"backend":"http://10.0.2.1:9000/base","protocol":"http","timeout_seconds":30,"rewrite":null},
            {"path":"/api/broker/","wildcard":true,"backend":"mqtt://10.0.2.2:1883","protocol":"http","timeout_seconds":30,"rewrite":null,"never_sent":true},
            {"path":"/api/bare/","wildcard":true,"backend":"localhost:50051","protocol":"grpc","timeout_seconds":30,"rewrite":null,"never_sent":true},
            {"path":"/api/unsaid/","wildcard":true,"backend":"mqtt://10.0.2.2:1883","protocol":"http","timeout_seconds":30,"rewrite":null}]"#,
            "",
        );
        let routed = |uri: &str| {
            let req = Request::builder().uri(uri).header("x-api-key", KEY).body(()).unwrap();
            route(&snap, peer(), &req, 100).and_then(|n| n.backend).map(|b| (b.upstream.text.clone(), format!("{}://{}{}", b.upstream.scheme, b.upstream.authority, b.upstream.prefix), b.never_sent))
        };
        // A WebSocket address is spoken to in HTTP, under its own name and so with a breaker of its own.
        assert_eq!(routed("/api/sockets/a"), Some(("ws://10.0.2.1:9000/base".into(), "http://10.0.2.1:9000/base".into(), false)));
        assert_eq!(routed("/api/plain/a"), Some(("http://10.0.2.1:9000/base".into(), "http://10.0.2.1:9000/base".into(), false)));
        let backend = |uri: &str| route(&snap, peer(), &Request::builder().uri(uri).header("x-api-key", KEY).body(()).unwrap(), 100).and_then(|n| n.backend).unwrap();
        assert!(!std::ptr::eq(&backend("/api/sockets/a").breaker, &backend("/api/plain/a").breaker));
        // Any other scheme, or none: nothing is sent, as the snapshot says.
        assert_eq!(routed("/api/broker/a").map(|(text, _, never)| (text, never)), Some(("mqtt://10.0.2.2:1883".into(), true)));
        assert_eq!(routed("/api/bare/a").map(|(text, _, never)| (text, never)), Some(("localhost:50051".into(), true)));
        // Unless it says so, such a route is the Python gateway's.
        assert_eq!(routed("/api/unsaid/a"), None);
        assert_eq!(Unanswered::Unsupported.name(), "UnsupportedProtocol");
    }

    const AGENTS: &str = r#","agents":{"hiring":"http://10.0.0.10:8001","based":"http://10.0.0.11:8001/base","secure":"https://10.0.0.12"},"agent_failure":{"message_json":"\"Try again later.\"","page":["<p>Request <code>","</code></p>"]}"#;

    fn to_agent(snap: &Snapshot, req: &Request<()>) -> Option<String> {
        match decide(snap, peer(), req, 100, |_| false) {
            Decision::Agent(a) => Some(format!("{}{}", a.backend.upstream.authority, a.target)),
            _ => None,
        }
    }

    #[test]
    fn a_request_for_a_known_agent_is_forwarded_here_without_a_key() {
        let snap = governed(AGENTS, "");
        let sent = |uri: &str| to_agent(&snap, &request("GET", uri));
        assert_eq!(sent("/agents/hiring/a/b?x=1&y=two").as_deref(), Some("10.0.0.10:8001/a/b?x=1&y=two"));
        assert_eq!(sent("/agents/hiring").as_deref(), Some("10.0.0.10:8001/"));
        assert_eq!(sent("/agents/hiring/").as_deref(), Some("10.0.0.10:8001/"));
        assert_eq!(sent("/agents/hiring/a?").as_deref(), Some("10.0.0.10:8001/a"));
        assert_eq!(sent("/agents/based/a").as_deref(), Some("10.0.0.11:8001/base/a"));
        for method in ["POST", "PUT", "PATCH", "DELETE", "HEAD"] {
            assert!(to_agent(&snap, &request(method, "/agents/hiring/a")).is_some(), "{method}");
        }
        // An agent this side does not know or cannot reach, and no agent at all.
        for uri in ["/agents/nobody/a", "/agents/secure/a", "/agents/", "/agents", "/agents/hiring%2Fa", "/api/agents/hiring/a"] {
            assert!(sent(uri).is_none(), "{uri}");
        }
        assert!(to_agent(&governed("", ""), &request("GET", "/agents/hiring/a")).is_none());
    }

    #[test]
    fn an_agent_request_python_would_send_differently_is_left_to_python() {
        let snap = governed(AGENTS, "");
        for uri in ["/agents/hiring/a?q=%ff", "/agents/hiring/v\u{662}/a"] {
            assert!(to_agent(&snap, &request("GET", uri)).is_none(), "{uri}");
        }
        // A query is written again as the Python gateway writes the dictionary it read it into.
        for (query, sent) in [("x=1&x=2", "?x=1&x=2"), ("q=a+b", "?q=a%20b"), ("q=%7E", "?q=~"), ("flag", "?flag="), ("=1", "?=1"), ("a=1&&b=2", "?a=1&b=2"), ("&", "")] {
            assert_eq!(to_agent(&snap, &request("GET", &format!("/agents/hiring/a?{query}"))).as_deref(), Some(&*format!("10.0.0.10:8001/a{sent}")), "{query}");
        }
        // A path that names a version tells the agent that one, or none if it is not known.
        let told = |uri: &str| match decide(&snap, peer(), &request("GET", uri), 100, |_| false) {
            Decision::Agent(a) => Some((a.version, a.told, a.target)),
            _ => None,
        };
        assert_eq!(told("/agents/hiring/v2/a"), Some((Some("1"), Some("2"), "/v2/a".to_owned())));
        assert_eq!(told("/agents/hiring/v9"), Some((Some("1"), None, "/v9".to_owned())));
        assert!(to_agent(&snap, &request("OPTIONS", "/agents/hiring/a")).is_none());
        for (name, value) in [("origin", "http://localhost:3001"), ("upgrade", "websocket"), ("x-protocol", "mcp"),
                              ("te", "trailers"), ("trailer", "x"), ("proxy-authorization", "Basic eA==")] {
            let req = Request::builder().uri("/agents/hiring/a").header(name, value).body(()).unwrap();
            assert!(to_agent(&snap, &req).is_none(), "{name}");
        }
        // A header given twice is no reason, as the agent is sent every line; but for a header
        // the Python gateway sends on one line of.
        let twice = Request::builder().uri("/agents/hiring/a").header("accept", "text/plain").header("accept", "application/json").body(()).unwrap();
        assert!(to_agent(&snap, &twice).is_some());
        let framed = Request::builder().uri("/agents/hiring/a").header("host", "a").header("host", "b").body(()).unwrap();
        assert!(to_agent(&snap, &framed).is_none());
    }

    #[test]
    fn a_header_given_twice_goes_on_whole_unless_a_rule_changed_it() {
        let came = || {
            let mut headers = HeaderMap::new();
            for (name, value) in [("x-tag", "a"), ("x-other", "o"), ("x-tag", "b"), ("x-old", "1"), ("x-set", "s"), ("x-old", "2"), ("x-set", "t"),
                                  ("x-same", "a"), ("x-same", "b"), ("x-gone", "1"), ("x-gone", "2"), ("traceparent", "t1"), ("traceparent", "t2")] {
                headers.append(name, HeaderValue::from_static(value));
            }
            headers
        };
        let read = |headers: &HeaderMap| {
            let mut names: Vec<&str> = headers.keys().map(|name| name.as_str()).collect();
            names.sort_unstable();
            names.iter().map(|name| format!("{name}={}", headers.get_all(*name).iter().map(|v| v.to_str().unwrap()).collect::<Vec<_>>().join(",")))
                .collect::<Vec<_>>().join(" ")
        };
        let mut headers = came();
        let repeated = take_repeated(&mut headers);
        assert_eq!(read(&headers), "traceparent=t1 x-gone=1 x-old=1 x-other=o x-same=a x-set=s x-tag=a");
        assert_eq!(repeated.len(), 6);
        // Renamed, set, removed: what the rule left. Set to the first line's own value, or not
        // named at all: every line. The gateway's own trace: its one line.
        let rule: Actions = serde_json::from_str(r#"{"rename":[["X-Old","X-New"]],"set":[["x-set","z"],["x-same","a"]],"remove":["X-Gone"]}"#).unwrap();
        let trace = Trace { request_id: "id".to_owned(), traceparent: "tp".to_owned() };
        let sent = Sent { trace: &trace, version: None, notices: None, inject_request_id: false, protocol: "http", path: None };
        assert!(shape_headers(&mut headers, &[&rule], Some(&sent), &repeated));
        assert_eq!(read(&headers), "traceparent=tp x-forwarded-for= x-new=1 x-other=o x-protocol=http x-same=a,b x-set=z x-tag=a,b");
        // With every path left to Python (a transformation rule exists, say), so is this one.
        assert!(to_agent(&governed(&format!(r#"{AGENTS},"fallthrough_prefixes":["/"]"#), ""), &request("GET", "/agents/hiring/a")).is_none());
    }

    #[test]
    fn a_query_python_writes_back_unchanged_is_told_apart() {
        for q in ["a=1", "a=1&b=two", "a=&b=x-y_z.~", "A1=B2"] {
            assert!(query_survives(q), "{q}");
        }
        for q in ["", "a", "a=1&a=2", "a=b=c", "a=1&", "a=%20", "a=b+c", "a=caf\u{e9}", "a[]=1"] {
            assert!(!query_survives(q), "{q}");
        }
    }

    #[test]
    fn an_agents_failure_is_answered_as_python_answers_it() {
        let snap = governed(AGENTS, "");
        let failure = snap.agent_failure.as_ref().unwrap();
        let t = trace_for(&HeaderMap::new());
        let resp = agent_failed(failure, false, &t, Duration::new(1_700_000_000, 123_000));
        assert_eq!(resp.status(), StatusCode::INTERNAL_SERVER_ERROR);
        assert_eq!(resp.headers()["x-content-type-options"], "nosniff");
        let body = body_of(resp);
        assert_eq!(body.as_object().unwrap().keys().collect::<Vec<_>>(), ["error", "message", "request_id", "timestamp"]);
        assert_eq!((&body["error"], &body["message"]), (&serde_json::json!("INTERNAL_ERROR"), &serde_json::json!("Try again later.")));
        // A browser gets the page, with the request id written in safely.
        let mut named = HeaderMap::new();
        named.insert("x-request-id", HeaderValue::from_static("a<b>&\"c'"));
        let resp = agent_failed(failure, true, &trace_for(&named), Duration::ZERO);
        assert_eq!(resp.headers()["content-type"], "text/html; charset=utf-8");
        assert_eq!(bytes_of(resp), "<p>Request <code>a&lt;b&gt;&amp;&quot;c&#x27;</code></p>");
    }

    #[test]
    fn a_browser_is_told_apart_as_python_tells_it() {
        let with = |pairs: &[(&str, &str)]| {
            let mut h = HeaderMap::new();
            for (k, v) in pairs {
                h.insert(HeaderName::from_bytes(k.as_bytes()).unwrap(), HeaderValue::from_str(v).unwrap());
            }
            prefers_html(&h)
        };
        assert_eq!(with(&[]), Some(false));
        assert_eq!(with(&[("accept", "*/*")]), Some(false));
        assert_eq!(with(&[("accept", "text/html,application/xhtml+xml,*/*;q=0.8")]), Some(true));
        assert_eq!(with(&[("accept", "Text/HTML")]), Some(true));
        assert_eq!(with(&[("accept", "application/json, text/html")]), Some(false));
        assert_eq!(with(&[("accept", "text/html, application/json")]), Some(true));
        assert_eq!(with(&[("accept", "text/html"), ("x-requested-with", "XMLHttpRequest")]), Some(false));
        assert_eq!(html_escaped("a<b>&\"c'"), "a&lt;b&gt;&amp;&quot;c&#x27;");
        // Without the gateway's word on how a failure is answered, agents are Python's.
        let agents_only = r#","agents":{"hiring":"http://10.0.0.10:8001"}"#;
        assert!(to_agent(&governed(agents_only, ""), &request("GET", "/agents/hiring/a")).is_none());
        let browser = Request::builder().uri("/agents/hiring/a").header("accept", "text/html").body(()).unwrap();
        assert!(to_agent(&governed(AGENTS, ""), &browser).is_some());
    }

    const GRPC: &str = r#","grpc":"http://127.0.0.1:50051""#;

    fn rpc(path: &str, headers: &[(&str, &str)]) -> Request<()> {
        let mut b = Request::builder().method("POST").uri(path).version(Version::HTTP_2).header("content-type", "application/grpc");
        for (k, v) in headers {
            b = b.header(*k, *v);
        }
        b.body(()).unwrap()
    }

    #[test]
    fn a_grpc_call_is_told_by_its_content_type_over_http2() {
        assert!(is_grpc(&rpc("/pkg.Service/Method", &[])));
        for kind in ["application/grpc+proto", "application/grpc+json", "application/grpc;charset=utf-8"] {
            let req = Request::builder().method("POST").uri("/a/b").version(Version::HTTP_2).header("content-type", kind).body(()).unwrap();
            assert!(is_grpc(&req), "{kind}");
        }
        for kind in ["application/grpc-web", "application/grpc-web+proto", "application/grpcx", "application/json"] {
            let req = Request::builder().method("POST").uri("/a/b").version(Version::HTTP_2).header("content-type", kind).body(()).unwrap();
            assert!(!is_grpc(&req), "{kind}");
        }
        // Not over HTTP/1, and not with another method.
        assert!(!is_grpc(&Request::builder().method("POST").uri("/a/b").header("content-type", "application/grpc").body(()).unwrap()));
        assert!(!is_grpc(&Request::builder().uri("/a/b").version(Version::HTTP_2).header("content-type", "application/grpc").body(()).unwrap()));
    }

    fn admitted(snap: &Snapshot, req: &Request<()>, now: u64, trusted: bool) -> Result<bool, GrpcRefusal> {
        admit_grpc(snap, peer(), req, now, trusted).map(|call| call.key_in_authorization)
    }

    #[test]
    fn a_grpc_call_with_a_working_key_goes_to_the_grpc_backend() {
        let snap = governed(GRPC, "");
        let call = admit_grpc(&snap, peer(), &rpc("/pkg.Service/Method", &[("x-api-key", KEY)]), 100, true).ok().unwrap();
        assert_eq!(call.native.protocol, Protocol::Grpc);
        assert_eq!(call.native.backend.unwrap().upstream.text, "http://127.0.0.1:50051");
        assert!(!call.key_in_authorization);
        let bearer = format!("Bearer {KEY}");
        assert_eq!(admitted(&snap, &rpc("/pkg.Service/Method", &[("authorization", &bearer)]), 100, true), Ok(true));
        // With both, the key is the one in its own header and the other is the caller's own.
        assert_eq!(admitted(&snap, &rpc("/a/b", &[("x-api-key", KEY), ("authorization", "Bearer theirs")]), 100, true), Ok(false));
    }

    #[test]
    fn a_grpc_call_this_side_cannot_be_sure_of_is_refused() {
        let with_key = rpc("/pkg.Service/Method", &[("x-api-key", KEY)]);
        let snap = governed(GRPC, "");
        assert_eq!(admitted(&snap, &with_key, 100, false), Err(GrpcRefusal::NOT_READY));
        assert_eq!(admitted(&governed("", ""), &with_key, 100, true), Err(GrpcRefusal::NOT_CONFIGURED));
        assert_eq!(admitted(&governed(r#","grpc":"https://secure.example:443""#, ""), &with_key, 100, true), Err(GrpcRefusal::NOT_CONFIGURED));
        assert_eq!(admitted(&snap, &rpc("/pkg.Service/Method", &[]), 100, true), Err(GrpcRefusal::NO_KEY));
        assert_eq!(admitted(&snap, &rpc("/pkg.Service/Method", &[("x-api-key", "sk-nope")]), 100, true), Err(GrpcRefusal::BAD_KEY));
        assert_eq!(admitted(&snap, &rpc("/pkg.Service/Method", &[("authorization", "Bearer sk-nope")]), 100, true), Err(GrpcRefusal::BAD_KEY));
        assert_eq!(admitted(&snap, &rpc("/pkg.Service/M%65thod", &[("x-api-key", KEY)]), 100, true), Err(GrpcRefusal::DENIED));
        // A key that is off or expired (there is no second opinion to ask, so none on the margin).
        for key_extra in [r#","is_active":false"#, r#","expires_at":100"#, r#","expires_at":99"#] {
            assert_eq!(admitted(&governed(GRPC, key_extra), &with_key, 100, true), Err(GrpcRefusal::BAD_KEY), "{key_extra}");
        }
        assert_eq!(admitted(&governed(GRPC, r#","expires_at":101"#), &with_key, 100, true), Ok(false));
        assert_eq!(admitted(&governed(GRPC, r#","native_ok":false"#), &with_key, 100, true), Err(GrpcRefusal::NOT_APPLICABLE));
        // With every path left to Python (a transformation rule exists, say), there is nobody to leave a call to.
        let all_python = format!(r#"{GRPC},"fallthrough_prefixes":["/"]"#);
        assert_eq!(admitted(&governed(&all_python, ""), &with_key, 100, true), Err(GrpcRefusal::NOT_APPLICABLE));
    }

    #[test]
    fn a_grpc_key_only_reaches_its_allowed_methods() {
        let snap = governed(GRPC, r#","allowed_paths":{"rules":[["under","/pkg.Service"]],"patterns_json":"[\"/pkg.Service\"]"}"#);
        let at = Duration::from_secs(100);
        let allowed = rpc("/pkg.Service/Method", &[("x-api-key", KEY)]);
        let call = admit_grpc(&snap, peer(), &allowed, 100, true).ok().unwrap();
        assert!(refusal(&snap, &call.native, allowed.uri().path(), at).is_none());
        let other = rpc("/pkg.Other/Method", &[("x-api-key", KEY)]);
        let call = admit_grpc(&snap, peer(), &other, 100, true).ok().unwrap();
        assert!(matches!(refusal(&snap, &call.native, other.uri().path(), at), Some(Refusal::Scope(_))));
    }

    #[test]
    fn a_grpc_refusal_is_one_a_grpc_client_reads() {
        let t = trace_for(&HeaderMap::new());
        let mut made = rate_limited("minute", 3, 60, None, &t);
        stamp(&mut made, &t, 0.001, None, None);
        let resp = GrpcRefusal::for_error("RATE_LIMIT_EXCEEDED").onto(made);
        assert_eq!(resp.status(), StatusCode::OK);
        let h = resp.headers();
        assert_eq!((h["content-type"].as_bytes(), h["grpc-status"].as_bytes()), (&b"application/grpc"[..], &b"8"[..]));
        assert_eq!(h["grpc-message"], "rate limit exceeded");
        assert!(h.contains_key("retry-after") && h.contains_key("x-request-id") && !h.contains_key("content-length"));
        assert_eq!(resp.body().size_hint().exact(), Some(0));
        assert_eq!(GrpcRefusal::for_error("AUTHORIZATION_ERROR").code, 7);
        assert_eq!(GrpcRefusal::for_error("SERVICE_UNAVAILABLE").code, 14);
        assert_eq!(GrpcRefusal::for_error("BACKEND_ERROR").code, 14);
    }

    #[test]
    fn a_request_from_another_origin_is_left_to_python_unless_this_side_knows_the_answer() {
        let from = |snap: &Snapshot, origins: &[&str]| {
            let mut b = Request::builder().uri("/api/users").header("x-api-key", KEY);
            for o in origins {
                b = b.header("origin", *o);
            }
            outcome(snap, &b.body(()).unwrap(), 100, false)
        };
        // No word from the control plane on how such requests are answered.
        assert_eq!(from(&governed("", ""), &["https://app.example"]), "ask");
        let told = governed(r#","cross_origin":{"any_origin":false,"origins":["https://app.example"],"credentials":true}"#, "");
        assert_eq!(from(&told, &["https://app.example"]), "native");
        assert_eq!(from(&told, &["https://other.example"]), "native");
        assert_eq!(from(&told, &["https://app.example", "https://app.example"]), "ask");
        assert_eq!(from(&told, &[]), "native");
    }

    fn granted(rules: &str, headers: &[(&str, &str)]) -> Result<Option<Granted>, ()> {
        let snap = Snapshot::from_json(&format!(r#"{{"keys":[],"cross_origin":{rules}}}"#)).unwrap();
        let mut h = HeaderMap::new();
        for (k, v) in headers {
            h.append(HeaderName::from_bytes(k.as_bytes()).unwrap(), HeaderValue::from_str(v).unwrap());
        }
        cross_origin(&snap, &h)
    }

    #[test]
    fn an_origin_earns_what_the_cors_middleware_gives_it() {
        let listed = r#"{"any_origin":false,"origins":["https://app.example"],"credentials":true}"#;
        let app = HeaderValue::from_static("https://app.example");
        assert_eq!(granted(listed, &[]), Ok(None));
        assert_eq!(granted(listed, &[("origin", "https://app.example")]), Ok(Some(Granted { any_origin: false, credentials: true, named: Some(app.clone()) })));
        // An origin that is not listed is still told about credentials, and nothing else.
        assert_eq!(granted(listed, &[("origin", "https://APP.example")]), Ok(Some(Granted { any_origin: false, credentials: true, named: None })));
        let any = r#"{"any_origin":true,"origins":[],"credentials":false}"#;
        assert_eq!(granted(any, &[("origin", "https://x.example")]), Ok(Some(Granted { any_origin: true, credentials: false, named: None })));
        // With a cookie, the star gives way to the origin itself.
        let x = HeaderValue::from_static("https://x.example");
        assert_eq!(granted(any, &[("origin", "https://x.example"), ("cookie", "s=1")]), Ok(Some(Granted { any_origin: true, credentials: false, named: Some(x) })));
        assert_eq!(granted("null", &[("origin", "https://x.example")]), Err(()));
        assert_eq!(granted("null", &[]), Ok(None));
    }

    #[test]
    fn the_cross_origin_headers_go_over_the_backends_own() {
        let headers_of = |g: &Granted, sent: &[(&str, &str)]| {
            let mut resp = Response::new(full(Bytes::new()));
            for (k, v) in sent {
                resp.headers_mut().append(HeaderName::from_bytes(k.as_bytes()).unwrap(), HeaderValue::from_str(v).unwrap());
            }
            g.onto(&mut resp);
            let mut all: Vec<String> = resp.headers().iter().map(|(k, v)| format!("{k}: {}", v.to_str().unwrap())).collect();
            all.sort();
            all
        };
        let named = Granted { any_origin: false, credentials: true, named: Some(HeaderValue::from_static("https://app.example")) };
        assert_eq!(headers_of(&named, &[]), ["access-control-allow-credentials: true", "access-control-allow-origin: https://app.example", "vary: Origin"]);
        assert_eq!(
            headers_of(&named, &[("vary", "Accept-Encoding"), ("vary", "Cookie"), ("access-control-allow-origin", "https://backend.example")]),
            ["access-control-allow-credentials: true", "access-control-allow-origin: https://app.example", "vary: Accept-Encoding, Origin"]
        );
        // Not listed: the backend's own answer to the question stands.
        let unlisted = Granted { any_origin: false, credentials: true, named: None };
        assert_eq!(
            headers_of(&unlisted, &[("vary", "Accept-Encoding"), ("access-control-allow-origin", "https://backend.example")]),
            ["access-control-allow-credentials: true", "access-control-allow-origin: https://backend.example", "vary: Accept-Encoding"]
        );
        let any = Granted { any_origin: true, credentials: false, named: None };
        assert_eq!(headers_of(&any, &[("access-control-allow-origin", "https://backend.example")]), ["access-control-allow-origin: *"]);
    }

    #[test]
    fn an_http2_request_reads_like_an_http1_one() {
        let mut req = Request::builder().uri("http://gw.example:8000/api/users?a=1").version(Version::HTTP_2).body(()).unwrap();
        as_http1_names_it(&mut req);
        assert_eq!(req.uri().to_string(), "/api/users?a=1");
        assert_eq!(req.headers()[HOST], "gw.example:8000");
        // A host the client also sent as a header is kept as sent.
        let mut req = Request::builder().uri("http://gw.example/").header("host", "other").body(()).unwrap();
        as_http1_names_it(&mut req);
        assert_eq!((req.uri().to_string().as_str(), &req.headers()[HOST]), ("/", &HeaderValue::from_static("other")));
    }

    #[test]
    fn a_backend_keeps_its_breaker_across_snapshots() {
        let known = Backends::default();
        let (mut one, mut two) = (governed(r#","backends":{"mcp":"http://b:1"}"#, ""), governed(r#","backends":{"mcp":"http://b:1/","acp":"http://b:2"}"#, ""));
        known.resolve(&mut one);
        known.resolve(&mut two);
        assert!(Arc::ptr_eq(one.routes[0].as_ref().unwrap(), two.routes[0].as_ref().unwrap()));
        assert!(!Arc::ptr_eq(two.routes[0].as_ref().unwrap(), two.routes[3].as_ref().unwrap()));
    }

    #[test]
    fn an_upstream_is_parsed_once() {
        let u = Upstream::parse("http://127.0.0.1:9100/").unwrap();
        assert_eq!((u.text.as_str(), u.prefix.as_str(), u.authority.as_str()), ("http://127.0.0.1:9100", "", "127.0.0.1:9100"));
        assert_eq!(Upstream::parse("http://backend:80/base/").unwrap().prefix, "/base");
        assert!(Upstream::parse("https://backend").is_err());
        assert!(Upstream::parse("backend:80").is_err());
    }
}
