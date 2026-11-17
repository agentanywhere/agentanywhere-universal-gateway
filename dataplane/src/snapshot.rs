//! The working set the control plane gives the data plane, held in memory.
//!
//! Keys are stored only as SHA-256 digests (what the database stores), so the data plane
//! never holds a usable credential. The control plane compiles everything that decides a
//! request (key scopes, tenants and their quotas, IP rules, policies) into the forms used here;
//! anything it cannot express that way it marks as not native, and the Python gateway keeps it.
use crate::boundary::{Boundary, BoundaryRecord};
use crate::breaker;
use crate::govern::{IpRule, IpRules, Net, Policy, Scope, ScopeRule};
use crate::limits::Window;
use crate::proxy::{Backend, ROUTED};
use crate::pyjson;
use crate::tenant::{Tenant, Usage};
use hyper::header::{HeaderName, HeaderValue};
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::hash::{BuildHasherDefault, Hasher};
use std::sync::atomic::AtomicU64;
use std::sync::Arc;
use std::time::Duration;

/// Keys are already SHA-256 digests, so the first eight bytes are a perfectly good hash.
#[derive(Default)]
pub struct DigestHasher(u64);

impl Hasher for DigestHasher {
    fn finish(&self) -> u64 {
        self.0
    }
    fn write(&mut self, bytes: &[u8]) {
        if bytes.len() >= 8 {
            self.0 = u64::from_le_bytes(bytes[..8].try_into().unwrap());
        }
    }
}

pub type Digest32 = [u8; 32];

pub struct KeyEntry {
    /// The key's id in the control plane's database; reported with each request. For an OAuth2
    /// token, what the Python gateway counts its refusals under.
    pub id: Arc<str>,
    /// For an OAuth2 token, the name the Python gateway gives its caller (`oauth2:<provider>`).
    /// `None` for a key.
    pub token: Option<Arc<str>>,
    pub is_active: bool,
    pub expires_at: Option<i64>,
    /// False when something that applies to the key cannot be decided here exactly as the Python
    /// gateway decides it: its requests are handed to the control plane.
    pub native_ok: bool,
    /// The key's tenant, when it has an active one.
    pub tenant: Option<Arc<Tenant>>,
    /// The key's `allowed_paths`, when it has any.
    pub scope: Option<Scope>,
    /// Epoch seconds of the key's latest native request, zero once reported. Shared across
    /// snapshot updates.
    pub last_used: Arc<AtomicU64>,
    pub per_minute: u32,
    pub per_hour: u32,
    /// Shared across snapshot updates so a sync never resets a key's counters.
    pub minute: Arc<Window>,
    pub hour: Arc<Window>,
}

/// What the gateway announces with a version that is on its way out: to the backend it tells
/// the version, and to the caller on every answer.
#[derive(Debug, PartialEq)]
pub struct Notices {
    /// `Warning`: the version is deprecated.
    pub warning: Option<HeaderValue>,
    /// `Sunset`: when it goes.
    pub sunset: Option<HeaderValue>,
    /// `Deprecation: true`, to the caller alone.
    pub deprecation: bool,
}

/// API versions as the data plane needs them: which are served here, and what is announced.
pub struct ApiVersions {
    pub default: Option<String>,
    /// version -> true when a request under it may be served here: it is not removed, not
    /// within a minute of its sunset, and has no backend of its own. Any other is handled by
    /// the control plane.
    pub native_ok: HashMap<String, bool>,
    /// What is announced with the versions that announce something.
    pub notices: HashMap<String, Notices>,
    /// The versions that are gone (removed, or past their sunset), each with what the gateway
    /// says of it in its 410. Such a version is not echoed to the caller either.
    pub gone: HashMap<String, String>,
    /// The versions with a backend of their own, by its address and whether it is one no
    /// request is ever sent to (`DynamicRoute::never_sent`).
    pub backend_urls: HashMap<String, (String, bool)>,
    /// Those backends, once resolved (`Backends::resolve`).
    pub backends: HashMap<String, std::sync::Arc<Backend>>,
    /// A version a path names is taken off the path a backend is asked for.
    pub strip_prefix: bool,
}

impl ApiVersions {
    /// What is announced with a version, when anything is.
    pub fn announced(&self, version: Option<&str>) -> Option<&Notices> {
        self.notices.get(version?)
    }

    /// The version as it is echoed to a caller: one that is gone is not.
    pub fn shown<'v>(&self, version: Option<&'v str>) -> Option<&'v str> {
        version.filter(|v| !self.gone.contains_key(*v))
    }
}

pub struct Snapshot {
    /// The stock request-id plugin is on: forward an `X-Request-ID` the client did not send.
    pub inject_request_id: bool,
    /// The control plane's revision of this content, sent back as `since` to ask "anything new?".
    pub revision: Option<String>,
    pub api_versions: ApiVersions,
    /// The headers the Python gateway's HTTP client adds to a request that carries none of that
    /// name (`Accept`, `User-Agent`), which this side adds too.
    pub client_headers: Vec<(HeaderName, HeaderValue)>,
    pub rate_limit_enabled: bool,
    pub circuit_breaker: breaker::Config,
    /// How long the backend may take to start answering before the request fails with a 502.
    pub upstream_timeout: Duration,
    pub keys: HashMap<Digest32, KeyEntry, BuildHasherDefault<DigestHasher>>,
    /// The OAuth2 access tokens the Python gateway accepts as `Authorization: Bearer <token>`, by
    /// digest. A token has no tenant, no allowed paths, and passes over IP rules and policies
    /// there, so the control plane lists none while any is in force.
    pub tokens: HashMap<Digest32, KeyEntry, BuildHasherDefault<DigestHasher>>,
    pub tenants: HashMap<String, Arc<Tenant>>,
    /// Peers whose `X-Forwarded-For` is believed.
    pub trusted_proxies: Vec<Net>,
    pub ip_rules: IpRules,
    /// In the order the Python engine evaluates them.
    pub policies: Vec<Policy>,
    /// Path prefixes the control plane must handle (dynamic routes, transformation rules, ...).
    pub fallthrough_prefixes: Vec<String>,
    /// The beginnings of the paths the Python gateway answers itself (health, metrics, admin,
    /// OAuth2, ...). With these, a path with no protocol's prefix that is none of them goes to the
    /// HTTP backend, as it does there. `None`: the control plane has not said, or sends such paths
    /// somewhere else, and they are its to answer.
    pub own_paths: Option<Vec<String>>,
    /// Where each protocol in `ROUTED` is sent, as the control plane has it configured.
    pub protocol_urls: [Option<String>; 5],
    /// The backends at those addresses, filled in when the snapshot is put to use. A protocol
    /// without one is the control plane's to serve.
    pub routes: [Option<Arc<Backend>>; 5],
    /// What the Python gateway checks in a request body before forwarding it.
    pub boundary: Boundary,
    /// The header a key is read from first; `Authorization` is always the second place.
    pub key_header: HeaderName,
    /// How a request without a usable credential is refused. `None` when only the Python gateway
    /// can tell (a plugin may authenticate the request some other way): it then answers them all.
    pub refusals: Option<Refusals>,
    /// What the gateway's health path answers with. `None` when the control plane did not say.
    pub ops: Option<Ops>,
    /// The WebSocket server `/ws/...` connections go to (a `ws://` address), when this side may
    /// relay them itself.
    pub websocket_url: Option<String>,
    /// The backend at that address, filled in when the snapshot is put to use.
    pub websocket: Option<Arc<Backend>>,
    /// The gRPC server calls are relayed to (an `http://` address spoken to in HTTP/2).
    pub grpc_url: Option<String>,
    /// The backend at that address, filled in when the snapshot is put to use.
    pub grpc: Option<Arc<Backend>>,
    /// The JSON-to-gRPC bridge under `/grpc`, when this side may serve it as the Python gateway does.
    pub bridge: Option<crate::bridge::Bridge>,
    /// The MQTT broker `POST /mqtt/publish` publishes to, while the Python gateway is connected
    /// to it and this side may publish there as well.
    pub mqtt: Option<Broker>,
    /// The agent edge: slug to upstream address (`http://`), for the agents this side forwards to.
    pub agent_urls: HashMap<String, String>,
    /// The backends at those addresses, filled in when the snapshot is put to use.
    pub agents: HashMap<String, Arc<Backend>>,
    /// How a failed agent is answered. Without it, agents are the Python gateway's.
    pub agent_failure: Option<AgentFailure>,
    /// The dynamic routes in force, in the order the Python gateway tries them.
    pub dynamic_routes: Vec<DynamicRoute>,
    /// `None` when the control plane did not say: requests from other origins are then its own.
    pub cross_origin: Option<CrossOrigin>,
    /// The pages a browser is shown for the errors answered here, by `<status>:<hint><countdown>`.
    /// `None` when the control plane sent none: browsers are then its own.
    pub error_pages: Option<HashMap<String, String>>,
    /// The transformation rules in force, in the order they are applied. A request one applies
    /// to is changed here if this side was told what the rule does, and is the Python gateway's
    /// otherwise. `None`: there are rules this side was not told the reach of, and every request is.
    pub transform_rules: Option<Vec<TransformRule>>,
}

/// The gateway's health endpoint, as the Python gateway serves it.
#[derive(Deserialize)]
pub struct Ops {
    /// Where it lives. `None` when only the Python gateway can answer there.
    pub health_path: Option<String>,
    /// The version it reports, as a JSON string.
    pub version_json: String,
}

fn no_rules() -> Option<Vec<TransformRule>> {
    Some(Vec::new())
}

/// What a transformation rule does to one side of an exchange, as the Python gateway does it
/// (`_apply_header_ops`, `_apply_query_ops`, the status map).
#[derive(Deserialize, Debug, Default, PartialEq)]
pub struct Actions {
    /// Headers to rename, `[old, new]`, done first.
    #[serde(default)]
    pub rename: Vec<(String, String)>,
    /// Headers to remove, in lower case, done second.
    #[serde(default)]
    pub remove: Vec<String>,
    /// Headers to set, `[name, value]`, done last.
    #[serde(default)]
    pub set: Vec<(String, String)>,
    /// Query parameters to remove, then to set. A request's only.
    #[serde(default)]
    pub query_remove: Vec<String>,
    #[serde(default)]
    pub query_set: Vec<(String, String)>,
    /// Statuses to turn into others, `[was, becomes]`. An answer's only.
    #[serde(default)]
    pub status: Vec<(u16, u16)>,
    /// Fields of a body that is a JSON object, by dotted path: to rename, `[old, new]`, then to
    /// remove, then to set, `[path, value]` with the value as JSON text. A request's only.
    #[serde(default)]
    body_rename: Vec<(String, String)>,
    #[serde(default)]
    body_remove: Vec<String>,
    #[serde(default)]
    body_set: Vec<(String, String)>,
    /// The same, ready to apply, in order.
    #[serde(skip)]
    body: Vec<BodyStep>,
    /// Text to replace in the path the backend is asked for. A request's only.
    path: Option<PathRewrite>,
}

/// A path rewrite. Without an expression: plain text (`from`) replaced by plain text (`to`),
/// either wherever it stands in the path or at its start only. With one (`regex`): each match
/// replaced by `parts`, text and the groups of the match, in order.
#[derive(Deserialize, Debug)]
pub struct PathRewrite {
    #[serde(default)]
    from: String,
    #[serde(default)]
    to: String,
    #[serde(default)]
    start: bool,
    regex: Option<String>,
    #[serde(default)]
    parts: Vec<Piece>,
    /// The expression, ready to apply.
    #[serde(skip)]
    expression: Option<regex::Regex>,
}

/// What a match is replaced by: text as it stands, or what a group of the match held (nothing,
/// for a group that took no part in it).
#[derive(Deserialize, Debug, PartialEq)]
#[serde(untagged)]
enum Piece {
    Text(String),
    Group(usize),
}

impl PartialEq for PathRewrite {
    fn eq(&self, other: &Self) -> bool {
        (&self.from, &self.to, self.start, &self.regex, &self.parts) == (&other.from, &other.to, other.start, &other.regex, &other.parts)
    }
}

impl PathRewrite {
    /// Make the rewrite ready to apply. `None` for one this side would not do as Python does:
    /// an expression its engine does not take, a group the expression does not have, or nothing
    /// to look for.
    fn ready(&mut self) -> Option<()> {
        let Some(pattern) = &self.regex else { return (self.start || !self.from.is_empty()).then_some(()) };
        let expression = regex::Regex::new(pattern).ok()?;
        if self.parts.iter().any(|piece| matches!(piece, Piece::Group(n) if *n >= expression.captures_len())) {
            return None;
        }
        self.expression = Some(expression);
        Some(())
    }

    fn apply(&self, path: &str) -> String {
        if let Some(expression) = &self.expression {
            // `re.sub`: matches from the left that do not overlap, each replaced in turn.
            let (mut out, mut from) = (String::with_capacity(path.len() + 16), 0);
            for found in expression.captures_iter(path) {
                let Some(whole) = found.get(0) else { continue };
                out.push_str(&path[from..whole.start()]);
                for piece in &self.parts {
                    match piece {
                        Piece::Text(text) => out.push_str(text),
                        Piece::Group(n) => out.push_str(found.get(*n).map_or("", |group| group.as_str())),
                    }
                }
                from = whole.end();
            }
            out.push_str(&path[from..]);
            return out;
        }
        match self.start {
            true => match path.strip_prefix(self.from.as_str()) {
                Some(rest) => format!("{}{rest}", self.to),
                None => path.to_owned(),
            },
            false => path.replace(self.from.as_str(), &self.to),
        }
    }
}

#[derive(Debug, PartialEq)]
enum BodyStep {
    Rename(pyjson::Path, pyjson::Path),
    Remove(pyjson::Path),
    Set(pyjson::Path, pyjson::Value),
}

impl Actions {
    pub fn changes_headers(&self) -> bool {
        !(self.rename.is_empty() && self.remove.is_empty() && self.set.is_empty())
    }

    pub fn changes_query(&self) -> bool {
        !(self.query_remove.is_empty() && self.query_set.is_empty())
    }

    pub fn changes_body(&self) -> bool {
        !self.body.is_empty()
    }

    pub fn changes_path(&self) -> bool {
        self.path.is_some()
    }

    /// Whether the path is rewritten by an expression, which the two sides read alike on ASCII.
    fn reads_path(&self) -> bool {
        self.path.as_ref().is_some_and(|p| p.regex.is_some())
    }

    /// Make the changes to a body ready to apply. `None` if this side cannot be certain of how
    /// Python writes a value the rule sets.
    pub fn ready(&mut self) -> Option<()> {
        if let Some(rewrite) = &mut self.path {
            rewrite.ready()?;
        }
        self.body.clear();
        self.body.extend(self.body_rename.iter().map(|(old, new)| BodyStep::Rename(pyjson::path(old), pyjson::path(new))));
        self.body.extend(self.body_remove.iter().map(|at| BodyStep::Remove(pyjson::path(at))));
        for (at, value) in &self.body_set {
            self.body.push(BodyStep::Set(pyjson::path(at), pyjson::parse(value.as_bytes())?));
        }
        Some(())
    }

    /// Apply the rule's changes to a body that is a JSON object, as `_apply_body_ops` does: a
    /// field that is there and not `null` moves to its new path, then fields are removed, then
    /// set (each to a copy of the rule's value).
    pub fn change_body(&self, body: &mut pyjson::Value) {
        for step in &self.body {
            match step {
                BodyStep::Rename(old, new) => {
                    if let Some(value) = body.get(old).cloned() {
                        body.remove(old);
                        body.set(new, value);
                    }
                }
                BodyStep::Remove(at) => body.remove(at),
                BodyStep::Set(at, value) => body.set(at, value.clone()),
            }
        }
    }

    /// Whether the rule reads or writes the header `name` (in lower case).
    pub fn names(&self, name: &str) -> bool {
        self.remove.iter().any(|n| n == name)
            || self.set.iter().any(|(n, _)| n.eq_ignore_ascii_case(name))
            || self.rename.iter().any(|(old, new)| old.eq_ignore_ascii_case(name) || new.eq_ignore_ascii_case(name))
    }
}

/// The header lines of a request or an answer as the Python gateway holds them while it applies
/// rules: a dictionary from name to value, in order, where names differ if their case does.
pub type HeaderLines<V> = Vec<(String, V)>;

pub fn put<V>(lines: &mut HeaderLines<V>, name: &str, value: V) {
    match lines.iter_mut().find(|(n, _)| n == name) {
        Some(line) => line.1 = value,
        None => lines.push((name.to_owned(), value)),
    }
}

/// Apply one rule's header changes as `_apply_header_ops` does: renames (the header is found
/// whatever its case, and moves to the end under the new name, or takes the place of a header
/// already called exactly that), then removals (whatever the case), then values set (under
/// exactly the name given, beside a header whose name differs only in case).
pub fn change_headers<V>(lines: &mut HeaderLines<V>, actions: &Actions, value: impl Fn(&str) -> Option<V>) {
    if !actions.rename.is_empty() {
        // Found by the names the headers had before any rename, the last of a name winning.
        let mut known: Vec<(String, String)> = Vec::new();
        for (name, _) in lines.iter() {
            let lower = name.to_ascii_lowercase();
            match known.iter_mut().find(|(l, _)| *l == lower) {
                Some(entry) => entry.1 = name.clone(),
                None => known.push((lower, name.clone())),
            }
        }
        for (old, new) in &actions.rename {
            let old = old.to_ascii_lowercase();
            let Some((_, name)) = known.iter().find(|(l, _)| *l == old) else { continue };
            let Some(at) = lines.iter().position(|(n, _)| n == name) else { continue };
            let (_, moved) = lines.remove(at);
            put(lines, new, moved);
        }
    }
    if !actions.remove.is_empty() {
        lines.retain(|(name, _)| !actions.remove.iter().any(|r| r.eq_ignore_ascii_case(name)));
    }
    for (name, text) in &actions.set {
        if let Some(v) = value(text) {
            put(lines, name, v);
        }
    }
}

/// A query as the Python gateway holds it on its way to a backend: a dictionary of one value to
/// a name, which its rules read and change, and beside it every value of each name given more
/// than once, put back before sending where no rule changed that name (`repeated_query`).
#[derive(Debug, Default, PartialEq)]
pub struct Query {
    /// One value to a name: the last given, in the place of the first.
    held: Vec<(String, String)>,
    /// The names given more than once, each with its values in the order given.
    repeated: Vec<(String, Vec<String>)>,
}

/// Read a query as the Python gateway reads it (`request.query_params`, which is `parse_qsl`
/// keeping blank values): the pieces between `&`, each a name and, after its first `=`, a
/// value; `+` a space and `%xx` the byte it names, an escape that is not one left as written.
/// Bytes that are not UTF-8 once unescaped are each read as U+FFFD, as Python's decoder reads
/// them with `errors="replace"`. `None` for a query beyond ASCII, which this side is not certain
/// to read the same.
pub fn read_query(query: &str) -> Option<Query> {
    if !query.is_ascii() {
        return None;
    }
    fn unquoted(text: &str) -> Option<String> {
        let hex = |b: u8| (b as char).to_digit(16).map(|d| d as u8);
        let bytes = text.as_bytes();
        let mut out = Vec::with_capacity(bytes.len());
        let mut i = 0;
        while i < bytes.len() {
            match (bytes[i], bytes.get(i + 1).copied().and_then(hex), bytes.get(i + 2).copied().and_then(hex)) {
                (b'%', Some(high), Some(low)) => {
                    out.push(high << 4 | low);
                    i += 3;
                }
                (b'+', _, _) => {
                    out.push(b' ');
                    i += 1;
                }
                (other, _, _) => {
                    out.push(other);
                    i += 1;
                }
            }
        }
        Some(String::from_utf8_lossy(&out).into_owned())
    }
    let mut all: Vec<(String, Vec<String>)> = Vec::new();
    for piece in query.split('&').filter(|piece| !piece.is_empty()) {
        let (name, value) = piece.split_once('=').unwrap_or((piece, ""));
        let (name, value) = (unquoted(name)?, unquoted(value)?);
        match all.iter_mut().find(|(n, _)| *n == name) {
            Some((_, values)) => values.push(value),
            None => all.push((name, vec![value])),
        }
    }
    let held = all.iter().map(|(name, values)| (name.clone(), values.last().cloned().unwrap_or_default())).collect();
    all.retain(|(_, values)| values.len() > 1);
    Some(Query { held, repeated: all })
}

/// Apply rules' query changes to a query as read, as `_apply_query_ops` does to the dictionary
/// the Python gateway reads a query into: removals, then values set, each rule in turn.
pub fn change_query(query: &mut Query, rules: &[&Actions]) {
    let pairs = &mut query.held;
    for actions in rules {
        pairs.retain(|(key, _)| !actions.query_remove.iter().any(|r| r == key));
        for (key, value) in &actions.query_set {
            match pairs.iter_mut().find(|(k, _)| k == key) {
                Some(pair) => pair.1.clone_from(value),
                None => pairs.push((key.clone(), value.clone())),
            }
        }
    }
}

/// A query as the Python gateway's HTTP client writes the dictionary (`urlencode` in httpx):
/// `name=value` joined by `&`, a name given more than once with every value where the
/// dictionary still holds its last (`_with_repeats`). A name or a value of letters, digits,
/// `-._~` and whole `%xx` escapes is written as it is; in any other, every byte that is not
/// one of those letters is escaped, `%` too. `None` when no parameter is left: the backend is
/// then asked with no `?`.
pub fn write_query(query: &Query) -> Option<String> {
    fn quoted(text: &str, out: &mut String) {
        let plain = |b: u8| b.is_ascii_alphanumeric() || matches!(b, b'-' | b'.' | b'_' | b'~');
        let bytes = text.as_bytes();
        let escape = |i: usize| bytes.get(i + 1).is_some_and(u8::is_ascii_hexdigit) && bytes.get(i + 2).is_some_and(u8::is_ascii_hexdigit);
        if bytes.iter().enumerate().all(|(i, b)| plain(*b) || (*b == b'%' && escape(i))) {
            out.push_str(text);
            return;
        }
        for b in bytes {
            match plain(*b) {
                true => out.push(*b as char),
                false => out.push_str(&format!("%{b:02X}")),
            }
        }
    }
    let mut out = String::new();
    let mut pair = |name: &str, value: &str| {
        if !out.is_empty() {
            out.push('&');
        }
        quoted(name, &mut out);
        out.push('=');
        quoted(value, &mut out);
    };
    for (name, value) in &query.held {
        match query.repeated.iter().find(|(n, values)| n == name && values.last() == Some(value)) {
            Some((_, values)) => values.iter().for_each(|each| pair(name, each)),
            None => pair(name, value),
        }
    }
    (!query.held.is_empty()).then_some(out)
}

/// The status an answer leaves with: each rule in turn maps the one it finds.
pub fn change_status(status: u16, rules: &[&Actions]) -> u16 {
    rules.iter().fold(status, |s, actions| actions.status.iter().find(|(was, _)| *was == s).map_or(s, |(_, becomes)| *becomes))
}

/// The rules that apply to one request, in order: what they do to it, and to its answer.
#[derive(Default)]
pub struct Shaping<'a> {
    pub request: Vec<&'a Actions>,
    pub response: Vec<&'a Actions>,
}

impl Shaping<'_> {
    pub fn is_empty(&self) -> bool {
        self.request.is_empty() && self.response.is_empty()
    }

    /// The path the backend is asked for once each rule in turn has rewritten it
    /// (`_apply_path_rewrite`), when any does. `Err` for a path this side might not rewrite as
    /// the Python gateway does: one beyond ASCII under an expression, where the two engines'
    /// classes differ.
    pub fn rewritten(&self, asked: &str) -> Result<Option<String>, ()> {
        if !asked.is_ascii() && self.request.iter().any(|a| a.reads_path()) {
            return Err(());
        }
        let mut path: Option<String> = None;
        for rewrite in self.request.iter().filter_map(|a| a.path.as_ref()) {
            path = Some(rewrite.apply(path.as_deref().unwrap_or(asked)));
        }
        Ok(path)
    }
}

/// A transformation rule: which requests it applies to and, when this side can do the same as
/// the Python gateway, what it does. The pattern arrives in one of the forms the Python gateway
/// describes it in, each matched here as it matches there.
#[derive(Deserialize, Debug)]
pub struct TransformRule {
    /// Whether this side was told all the rule does. If not, the Python gateway applies it.
    #[serde(default)]
    native: bool,
    request: Option<Actions>,
    response: Option<Actions>,
    /// Every path.
    #[serde(default)]
    all: bool,
    /// Plain characters, `*` for any run of characters (slashes included) and `?` for any one.
    /// It must match the whole path.
    glob: Option<String>,
    /// The same with character classes, each given as the characters it lets through.
    parts: Option<Vec<Part>>,
    /// A regular expression, matched from the start of the path.
    regex: Option<String>,
    /// Text the path must contain (what the Python gateway falls back to for a pattern that
    /// looks like a regular expression and is not one).
    contains: Option<String>,
    /// Upper-case method names; none means every method.
    #[serde(default)]
    methods: Vec<String>,
    #[serde(skip)]
    matcher: Option<Matcher>,
}

/// One part of a glob with character classes.
#[derive(Deserialize, Debug)]
#[serde(untagged)]
enum Part {
    /// `*` or `?`.
    Wild(String),
    Lit { lit: String },
    Set { set: String },
}

#[derive(Debug)]
enum Step {
    Byte(u8),
    One,
    Run,
    /// Which of the 128 ASCII characters are let through.
    Set(u128),
}

#[derive(Debug)]
enum Matcher {
    All,
    Glob(String),
    Steps(Vec<Step>),
    Regex(regex::Regex),
    Contains(String),
}

impl TransformRule {
    /// Work out how the rule's pattern is matched. `None` when it is in no form this side knows,
    /// or in more than one: then no rule can be trusted, and every request is the Python gateway's.
    fn ready(mut self) -> Option<TransformRule> {
        // A path is rewritten on the way to the backend only.
        let request = self.request.as_mut().map(Actions::ready).unwrap_or(Some(()));
        let response = self.response.as_mut().map(|a| a.ready().filter(|_| !a.changes_path())).unwrap_or(Some(()));
        if request.is_none() || response.is_none() {
            self.native = false;
        }
        let forms = [self.all, self.glob.is_some(), self.parts.is_some(), self.regex.is_some(), self.contains.is_some()];
        if forms.iter().filter(|f| **f).count() != 1 {
            return None;
        }
        self.matcher = Some(if self.all {
            Matcher::All
        } else if let Some(glob) = self.glob.take() {
            Matcher::Glob(glob)
        } else if let Some(parts) = self.parts.take() {
            let mut steps = Vec::new();
            for part in parts {
                match part {
                    Part::Wild(w) if w == "*" => steps.push(Step::Run),
                    Part::Wild(w) if w == "?" => steps.push(Step::One),
                    Part::Wild(_) => return None,
                    Part::Lit { lit } => steps.extend(lit.bytes().map(Step::Byte)),
                    Part::Set { set } if set.is_ascii() => steps.push(Step::Set(set.bytes().fold(0, |bits, b| bits | (1u128 << b)))),
                    Part::Set { .. } => return None,
                }
            }
            Matcher::Steps(steps)
        } else if let Some(pattern) = self.regex.take() {
            // The Python gateway asks whether the expression matches at the start of the path.
            Matcher::Regex(regex::Regex::new(&format!("^(?:{pattern})")).ok()?)
        } else {
            Matcher::Contains(self.contains.take()?)
        });
        Some(self)
    }

    /// Whether this side reads `path` as the rule's pattern reads it there. Beyond ASCII, the
    /// two engines' classes (`\w`, `\d`) take different characters.
    fn certain(&self, path: &str) -> bool {
        path.is_ascii() || !matches!(self.matcher, Some(Matcher::Regex(_)))
    }

    fn applies(&self, method: &str, path: &str) -> bool {
        if !(self.methods.is_empty() || self.methods.iter().any(|m| m == method)) {
            return false;
        }
        match &self.matcher {
            Some(Matcher::All) => true,
            Some(Matcher::Glob(glob)) => glob_matches(glob.as_bytes(), path.as_bytes()),
            Some(Matcher::Steps(steps)) => steps_match(steps, path.as_bytes()),
            Some(Matcher::Regex(regex)) => regex.is_match(path),
            Some(Matcher::Contains(text)) => path.contains(text.as_str()),
            // Never the case for a rule that was let into a snapshot.
            None => true,
        }
    }
}

/// `dataplane --judge-patterns`: each line on stdin is `{"rule": <a transformation rule as the
/// snapshot carries it>, "paths": [...]}`; the answer is a line with, for each path, whether the
/// rule's pattern matches it (`1` or `0`), or `ask` for a rule this side would not trust. The
/// conformance suite compares the lines with the Python gateway's own matching.
pub fn judge_patterns() -> Result<(), String> {
    use std::io::{BufRead, Write};

    #[derive(Deserialize)]
    struct Case {
        rule: TransformRule,
        paths: Vec<String>,
    }

    let out = std::io::stdout();
    let mut out = out.lock();
    for line in std::io::stdin().lock().lines() {
        let line = line.map_err(|e| e.to_string())?;
        let case: Case = serde_json::from_str(&line).map_err(|e| format!("case is not valid: {e}"))?;
        let said = match case.rule.ready() {
            Some(rule) => case.paths.iter().map(|p| if rule.applies("GET", p) { '1' } else { '0' }).collect(),
            None => "ask".to_owned(),
        };
        writeln!(out, "{said}").map_err(|e| e.to_string())?;
    }
    Ok(())
}

/// `glob_matches`, for a pattern with character classes.
fn steps_match(steps: &[Step], text: &[u8]) -> bool {
    let (mut s, mut t) = (0, 0);
    let mut star: Option<(usize, usize)> = None;
    while t < text.len() {
        let fits = match steps.get(s) {
            Some(Step::Byte(b)) => *b == text[t],
            Some(Step::One) => true,
            Some(Step::Set(bits)) => text[t] < 128 && bits & (1u128 << text[t]) != 0,
            Some(Step::Run) | None => false,
        };
        if fits {
            s += 1;
            t += 1;
        } else if matches!(steps.get(s), Some(Step::Run)) {
            star = Some((s, t));
            s += 1;
        } else if let Some((at, took)) = star {
            star = Some((at, took + 1));
            (s, t) = (at + 1, took + 1);
        } else {
            return false;
        }
    }
    steps[s..].iter().all(|step| matches!(step, Step::Run))
}

/// Whether `text` is matched in full by `pattern`, in which `*` stands for any run of bytes and
/// `?` for any one: Python's `fnmatch`, without its character classes.
pub fn glob_matches(pattern: &[u8], text: &[u8]) -> bool {
    let (mut p, mut t) = (0, 0);
    // Where to come back to when what follows a `*` does not fit: the `*`, and how much it took.
    let mut star: Option<(usize, usize)> = None;
    while t < text.len() {
        if p < pattern.len() && (pattern[p] == b'?' || (pattern[p] != b'*' && pattern[p] == text[t])) {
            p += 1;
            t += 1;
        } else if p < pattern.len() && pattern[p] == b'*' {
            star = Some((p, t));
            p += 1;
        } else if let Some((at, took)) = star {
            star = Some((at, took + 1));
            p = at + 1;
            t = took + 1;
        } else {
            return false;
        }
    }
    pattern[p..].iter().all(|c| *c == b'*')
}

/// How the Python gateway answers a request from a web page in another origin.
#[derive(Deserialize, Debug, PartialEq)]
pub struct CrossOrigin {
    /// Every origin is welcome (`*`).
    pub any_origin: bool,
    /// The origins named back to their own requests.
    pub origins: std::collections::HashSet<String>,
    /// Whether answers say credentials are allowed.
    pub credentials: bool,
    /// How a preflight is answered. `None`: the control plane has not said, and answers them.
    #[serde(default)]
    pub preflight: Option<Preflight>,
}

/// What the Python gateway's CORS middleware answers a preflight with, as it was set up.
#[derive(Deserialize, Debug, PartialEq)]
pub struct Preflight {
    /// The headers every answer starts from, in order.
    pub headers: Vec<(String, String)>,
    /// An origin that is welcome is named back (otherwise the headers already say `*`).
    pub explicit_origin: bool,
    pub methods: Vec<String>,
    /// Any header may be sent: the ones asked for are named back.
    pub all_headers: bool,
    /// Otherwise, the headers that may be sent, in lower case.
    pub allow_headers: Vec<String>,
}

/// A route registered through the admin API: where a matching request goes instead.
#[derive(Deserialize)]
pub struct DynamicRoute {
    /// The path it matches: all of it, or with `wildcard` its beginning.
    pub path: String,
    pub wildcard: bool,
    /// Where requests go (`http://`). `None` for a route only the Python gateway can follow.
    #[serde(rename = "backend")]
    pub backend_url: Option<String>,
    /// That backend, filled in when the snapshot is put to use.
    #[serde(skip)]
    pub backend: Option<Arc<Backend>>,
    /// The protocol name the backend is told.
    pub protocol: String,
    timeout_seconds: Option<u64>,
    /// The path the backend is asked for, in place of the caller's.
    pub rewrite: Option<String>,
    /// The backend is in a scheme the Python gateway's HTTP client does not speak: no request
    /// is sent, and each fails as a backend that cannot be reached does.
    #[serde(default)]
    pub never_sent: bool,
}

impl DynamicRoute {
    pub fn matches(&self, path: &str) -> bool {
        if self.wildcard {
            path.starts_with(self.path.as_str())
        } else {
            path == self.path
        }
    }

    /// How long the backend may take to start answering.
    pub fn timeout(&self) -> Option<Duration> {
        self.timeout_seconds.map(Duration::from_secs)
    }
}

/// The Python gateway's answer when an agent's connection fails before it answers.
#[derive(Deserialize, Debug, PartialEq)]
pub struct AgentFailure {
    /// The message of its 500, as a JSON string.
    pub message_json: String,
    /// The page a browser is shown, in two parts: the request id goes between them.
    pub page: (String, String),
}

/// An MQTT broker, reached with or without TLS.
#[derive(Deserialize, Clone, Debug, PartialEq)]
pub struct Broker {
    pub host: String,
    pub port: u16,
    #[serde(default)]
    pub tls: bool,
}

/// The Python gateway's own answer to `/ready`, as of one snapshot answer.
#[derive(Deserialize, Debug, PartialEq)]
pub struct Readiness {
    pub status: u16,
    pub body_json: String,
}

/// What comes with every control-plane answer, changed or not.
#[derive(Default)]
pub struct Extras {
    pub usage: Option<UsageBase>,
    pub ready: Option<Readiness>,
}

/// The Python gateway's words for one refusal, ready to put into its error envelope.
#[derive(Deserialize)]
pub struct Wording {
    pub message_json: String,
    pub details_json: String,
}

#[derive(Deserialize)]
pub struct Refusals {
    /// No credential was sent.
    pub missing_key: Wording,
    /// The credential is not a key that works.
    pub invalid_key: Wording,
}

#[derive(Deserialize)]
struct SnapshotFile {
    /// Protocol name -> backend address. Without it only plain HTTP is served natively.
    #[serde(default)]
    backends: HashMap<String, Option<String>>,
    boundary: Option<BoundaryRecord>,
    key_header: Option<String>,
    refusals: Option<Refusals>,
    ops: Option<Ops>,
    websocket: Option<String>,
    grpc: Option<String>,
    grpc_bridge: Option<crate::bridge::Bridge>,
    mqtt: Option<Broker>,
    #[serde(default)]
    agents: HashMap<String, String>,
    agent_failure: Option<AgentFailure>,
    #[serde(default)]
    dynamic_routes: Vec<DynamicRoute>,
    cross_origin: Option<CrossOrigin>,
    error_pages: Option<HashMap<String, String>>,
    /// Absent: a control plane that knows of no rules. `null`: rules it could not describe.
    #[serde(default = "no_rules")]
    transform_rules: Option<Vec<TransformRule>>,
    ready: Option<Readiness>,
    #[serde(default)]
    keys: Vec<KeyRecord>,
    #[serde(default)]
    tokens: Vec<TokenRecord>,
    #[serde(default)]
    fallthrough_prefixes: Vec<String>,
    #[serde(default)]
    own_paths: Option<Vec<String>>,
    #[serde(default = "yes")]
    rate_limit_enabled: bool,
    api_versions: Option<VersionsRecord>,
    #[serde(default)]
    client_headers: Option<Vec<(String, String)>>,
    revision: Option<String>,
    #[serde(default)]
    unchanged: bool,
    #[serde(default)]
    inject_request_id: bool,
    circuit_breaker: Option<BreakerRecord>,
    upstream_timeout_seconds: Option<f64>,
    #[serde(default)]
    tenants: HashMap<String, TenantRecord>,
    #[serde(default)]
    trusted_proxies: Vec<String>,
    #[serde(default)]
    ip_rules: Vec<IpRuleRecord>,
    #[serde(default)]
    policies: Vec<PolicyRecord>,
    tenant_usage: Option<UsageBase>,
}

#[derive(Deserialize)]
struct TenantRecord {
    minute: Option<u64>,
    hour: Option<u64>,
    details_json: String,
}

#[derive(Deserialize)]
struct IpRuleRecord {
    scope: String,
    scope_id: Option<String>,
    allow: bool,
    net: Option<String>,
    details_json: String,
}

#[derive(Deserialize)]
struct PolicyRecord {
    path: Option<String>,
    #[serde(default)]
    methods: Vec<String>,
    #[serde(default)]
    tenants: Vec<String>,
    cidrs: Option<Vec<String>>,
    time: Option<(u32, u32)>,
    deny: bool,
    message_json: String,
    details_json: String,
}

#[derive(Deserialize)]
struct ScopeRecord {
    rules: Vec<Vec<String>>,
    patterns_json: String,
}

/// What the database holds for each limited tenant in the current minute and hour.
#[derive(Deserialize)]
pub struct UsageBase {
    minute_start: u64,
    hour_start: u64,
    /// tenant id -> `[minute, hour]`; a tenant not listed has used nothing.
    #[serde(default)]
    used: HashMap<String, (u64, u64)>,
}

impl UsageBase {
    pub fn apply(&self, snap: &Snapshot) {
        for (id, t) in &snap.tenants {
            if t.minute.is_some() || t.hour.is_some() {
                let (minute, hour) = self.used.get(id).copied().unwrap_or((0, 0));
                t.usage.set_base(self.minute_start, self.hour_start, minute, hour);
            }
        }
    }
}

#[derive(Deserialize)]
struct BreakerRecord {
    enabled: bool,
    failure_threshold: u32,
    timeout_seconds: u64,
}

#[derive(Deserialize)]
struct VersionsRecord {
    default: Option<String>,
    #[serde(default)]
    versions: Vec<VersionRecord>,
    #[serde(default)]
    strip_prefix: bool,
}

#[derive(Deserialize)]
struct VersionRecord {
    version: String,
    #[serde(default = "yes")]
    native_ok: bool,
    #[serde(default)]
    notices: Option<NoticesRecord>,
    /// What the gateway says of a version that is gone.
    #[serde(default)]
    gone: Option<String>,
    #[serde(default)]
    backend: Option<String>,
    #[serde(default)]
    never_sent: bool,
}

#[derive(Deserialize)]
struct NoticesRecord {
    #[serde(default)]
    warning: Option<String>,
    #[serde(default)]
    sunset: Option<String>,
    #[serde(default)]
    deprecation: bool,
}

#[derive(Deserialize)]
struct KeyRecord {
    hash: String,
    id: String,
    #[serde(default = "yes")]
    is_active: bool,
    expires_at: Option<i64>,
    rate_limit_per_minute: u32,
    rate_limit_per_hour: u32,
    #[serde(default = "yes")]
    native_ok: bool,
    tenant: Option<String>,
    allowed_paths: Option<ScopeRecord>,
}

#[derive(Deserialize)]
struct TokenRecord {
    hash: String,
    id: String,
    name: String,
    expires_at: i64,
    rate_limit_per_minute: u32,
    rate_limit_per_hour: u32,
}

fn yes() -> bool {
    true
}

pub fn hash_key(key: &[u8]) -> Digest32 {
    Sha256::digest(key).into()
}

pub fn parse_hex32(s: &str) -> Option<Digest32> {
    let b = s.as_bytes();
    if b.len() != 64 {
        return None;
    }
    let nib = |c: u8| match c {
        b'0'..=b'9' => Some(c - b'0'),
        b'a'..=b'f' => Some(c - b'a' + 10),
        b'A'..=b'F' => Some(c - b'A' + 10),
        _ => None,
    };
    let mut out = [0u8; 32];
    for i in 0..32 {
        out[i] = (nib(b[2 * i])? << 4) | nib(b[2 * i + 1])?;
    }
    Some(out)
}

/// What a control-plane answer means: nothing new, or a complete new snapshot.
pub enum Parsed {
    Unchanged(Extras),
    Full(Snapshot, Extras),
}

fn net(text: &str) -> Result<Net, String> {
    Net::parse(text).ok_or_else(|| format!("{text} is not a network"))
}

fn scope(record: ScopeRecord) -> Result<Scope, String> {
    let mut rules = Vec::with_capacity(record.rules.len());
    for r in record.rules {
        let mut it = r.into_iter();
        rules.push(match (it.next().as_deref(), it.next()) {
            (Some("all"), None) => ScopeRule::All,
            (Some("prefix"), Some(text)) => ScopeRule::Prefix(text),
            (Some("under"), Some(base)) => ScopeRule::Under(base),
            (kind, _) => return Err(format!("unknown allowed-path rule {kind:?}")),
        });
    }
    Ok(Scope { rules, patterns_json: record.patterns_json })
}

impl Snapshot {
    /// Whether a transformation rule applies to a request: to what the backend is asked for
    /// (`asked`), or to its answer, which the Python gateway matches by the request's own path
    /// without the leading slash.
    pub fn transformed(&self, method: &str, asked: &str, path: &str) -> bool {
        let Some(rules) = &self.transform_rules else { return true };
        if rules.is_empty() {
            return false;
        }
        let answered = path.strip_prefix('/').unwrap_or(path);
        rules.iter().any(|r| !(r.certain(asked) && r.certain(answered)) || r.applies(method, asked) || r.applies(method, answered))
    }

    /// What the rules do to a request and to its answer, matched as `transformed` matches them.
    /// `None` when one that applies is the Python gateway's to apply.
    pub fn shaping(&self, method: &str, asked: &str, path: &str) -> Option<Shaping<'_>> {
        let rules = self.transform_rules.as_ref()?;
        let mut shaping = Shaping::default();
        if rules.is_empty() {
            return Some(shaping);
        }
        let answered = path.strip_prefix('/').unwrap_or(path);
        for rule in rules {
            if !(rule.certain(asked) && rule.certain(answered)) {
                return None;
            }
            let (before, after) = (rule.applies(method, asked), rule.applies(method, answered));
            if (before || after) && !rule.native {
                return None;
            }
            // A rule that changes nothing on a side is not in the way of anything there.
            let does = |a: &&Actions| a.changes_headers() || a.changes_query() || a.changes_body() || a.changes_path() || !a.status.is_empty();
            shaping.request.extend(rule.request.as_ref().filter(|_| before).filter(does));
            shaping.response.extend(rule.response.as_ref().filter(|_| after).filter(does));
        }
        Some(shaping)
    }
}

impl Snapshot {
    pub fn empty() -> Snapshot {
        Snapshot::from_json(r#"{"keys":[]}"#).expect("an empty snapshot is valid")
    }

    pub fn from_json(text: &str) -> Result<Snapshot, String> {
        match Snapshot::parse(text, None)? {
            Parsed::Full(s, extras) => {
                if let Some(u) = extras.usage {
                    u.apply(&s);
                }
                Ok(s)
            }
            Parsed::Unchanged(_) => Err("snapshot says unchanged but none was loaded".into()),
        }
    }

    /// Parse a control-plane answer. Counters of keys and tenants already in `prev` are carried
    /// over. Anything in it this side cannot read is an error: the snapshot held stays in force
    /// and, once it is too old, everything goes to the Python gateway.
    pub fn parse(text: &str, prev: Option<&Snapshot>) -> Result<Parsed, String> {
        let file: SnapshotFile = serde_json::from_str(text).map_err(|e| format!("snapshot is not valid: {e}"))?;
        // Both are put into answers as they are: they must be what they claim to be.
        if file.ready.as_ref().is_some_and(|r| !matches!(r.status, 200 | 503) || !r.body_json.starts_with('{')) {
            return Err("ready: not an answer to /ready".into());
        }
        if file.ops.as_ref().is_some_and(|o| !o.version_json.starts_with('"')) {
            return Err("ops: the version must be a JSON string".into());
        }
        let extras = Extras { usage: file.tenant_usage, ready: file.ready };
        if file.unchanged {
            return Ok(Parsed::Unchanged(extras));
        }
        let mut tenants: HashMap<String, Arc<Tenant>> = HashMap::with_capacity(file.tenants.len());
        for (id, t) in file.tenants {
            if !t.details_json.starts_with('{') {
                return Err(format!("tenant {id}: details must be a JSON object"));
            }
            let usage = prev.and_then(|p| p.tenants.get(&id)).map(|t| t.usage.clone()).unwrap_or_else(|| Arc::new(Usage::default()));
            let tenant = Tenant {
                id: Arc::from(id.as_str()),
                minute: t.minute,
                hour: t.hour,
                details_json: t.details_json,
                header: HeaderValue::from_str(&id).ok(),
                usage,
            };
            tenants.insert(id, Arc::new(tenant));
        }
        let trusted_proxies = file.trusted_proxies.iter().map(|t| net(t)).collect::<Result<Vec<_>, _>>()?;
        let mut ip_rules = IpRules::default();
        for r in file.ip_rules {
            if !r.details_json.starts_with('{') {
                return Err("IP rule: details must be a JSON object".into());
            }
            let rule = IpRule { allow: r.allow, net: r.net.as_deref().map(net).transpose()?, details_json: r.details_json };
            ip_rules.add(&r.scope, r.scope_id, rule)?;
        }
        let mut policies = Vec::with_capacity(file.policies.len());
        for p in file.policies {
            policies.push(Policy {
                path: p.path,
                methods: p.methods,
                tenants: p.tenants,
                cidrs: p.cidrs.map(|nets| nets.iter().map(|t| net(t)).collect::<Result<Vec<_>, _>>()).transpose()?,
                time: p.time,
                deny: p.deny,
                message_json: p.message_json,
                details_json: p.details_json,
            });
        }
        let mut keys: HashMap<Digest32, KeyEntry, BuildHasherDefault<DigestHasher>> = HashMap::default();
        for k in file.keys {
            let digest = parse_hex32(&k.hash).ok_or_else(|| format!("key {}: hash must be 64 hex characters", k.id))?;
            let reused = prev.and_then(|p| p.keys.get(&digest));
            let tenant = match &k.tenant {
                Some(id) => Some(tenants.get(id).cloned().ok_or_else(|| format!("key {}: unknown tenant {id}", k.id))?),
                None => None,
            };
            keys.insert(
                digest,
                KeyEntry {
                    id: Arc::from(k.id.as_str()),
                    token: None,
                    is_active: k.is_active,
                    expires_at: k.expires_at,
                    native_ok: k.native_ok,
                    tenant,
                    scope: k.allowed_paths.map(scope).transpose()?,
                    last_used: reused.map(|e| e.last_used.clone()).unwrap_or_default(),
                    per_minute: k.rate_limit_per_minute,
                    per_hour: k.rate_limit_per_hour,
                    minute: reused.map(|e| e.minute.clone()).unwrap_or_else(|| Arc::new(Window::new())),
                    hour: reused.map(|e| e.hour.clone()).unwrap_or_else(|| Arc::new(Window::new())),
                },
            );
        }
        let mut tokens: HashMap<Digest32, KeyEntry, BuildHasherDefault<DigestHasher>> = HashMap::default();
        for t in file.tokens {
            let digest = parse_hex32(&t.hash).ok_or("token: hash must be 64 hex characters")?;
            let reused = prev.and_then(|p| p.tokens.get(&digest));
            tokens.insert(
                digest,
                KeyEntry {
                    id: Arc::from(t.id.as_str()),
                    token: Some(Arc::from(t.name.as_str())),
                    is_active: true,
                    expires_at: Some(t.expires_at),
                    native_ok: true,
                    tenant: None,
                    scope: None,
                    last_used: Arc::default(),
                    per_minute: t.rate_limit_per_minute,
                    per_hour: t.rate_limit_per_hour,
                    minute: reused.map(|e| e.minute.clone()).unwrap_or_else(|| Arc::new(Window::new())),
                    hour: reused.map(|e| e.hour.clone()).unwrap_or_else(|| Arc::new(Window::new())),
                },
            );
        }
        // Without an `api_versions` section, the gateway's shipped defaults apply: versions 1 (default) and 2.
        let api_versions = match file.api_versions {
            Some(v) => {
                let (mut native_ok, mut notices, mut gone, mut backend_urls) = (HashMap::new(), HashMap::new(), HashMap::new(), HashMap::new());
                for record in v.versions {
                    if let Some(said) = record.gone.filter(|_| record.native_ok) {
                        gone.insert(record.version.clone(), said);
                    }
                    if let Some(url) = record.backend.filter(|_| record.native_ok) {
                        backend_urls.insert(record.version.clone(), (url, record.never_sent));
                    }
                    // A notice this side cannot write as a header leaves the version to the control plane.
                    let read = |text: Option<String>| text.map(|t| HeaderValue::from_str(&t)).transpose();
                    let told = match record.notices {
                        None => Some(None),
                        Some(n) => match (read(n.warning), read(n.sunset)) {
                            (Ok(warning), Ok(sunset)) => Some(Some(Notices { warning, sunset, deprecation: n.deprecation })),
                            _ => None,
                        },
                    };
                    native_ok.insert(record.version.clone(), record.native_ok && told.is_some());
                    if let Some(Some(told)) = told.filter(|_| record.native_ok) {
                        notices.insert(record.version, told);
                    }
                }
                ApiVersions { default: v.default, strip_prefix: v.strip_prefix, native_ok, notices, gone, backend_urls, backends: HashMap::new() }
            }
            None => ApiVersions {
                default: Some("1".into()),
                native_ok: [("1".to_string(), true), ("2".to_string(), true)].into_iter().collect(),
                notices: HashMap::new(),
                gone: HashMap::new(),
                backend_urls: HashMap::new(),
                backends: HashMap::new(),
                strip_prefix: false,
            },
        };
        let key_header = file.key_header.as_deref().unwrap_or("x-api-key");
        let key_header = HeaderName::from_bytes(key_header.to_ascii_lowercase().as_bytes())
            .map_err(|_| format!("{key_header} is not a header name"))?;
        if let Some(r) = &file.refusals {
            for w in [&r.missing_key, &r.invalid_key] {
                if !w.message_json.starts_with('"') || !w.details_json.starts_with('{') {
                    return Err("refusals: a message must be a JSON string and details a JSON object".into());
                }
            }
        }
        let snap = Snapshot {
            key_header,
            refusals: file.refusals,
            ops: file.ops,
            websocket_url: file.websocket.filter(|u| u.starts_with("ws://") || u.starts_with("wss://")),
            websocket: None,
            grpc_url: file.grpc.filter(|u| u.starts_with("http://") || u.starts_with("https://")),
            grpc: None,
            bridge: file.grpc_bridge.and_then(|mut b| {
                b.uri = b.target.strip_prefix("http://").and_then(|_| b.target.parse().ok())?;
                Some(b)
            }),
            mqtt: file.mqtt.filter(|b| !b.host.is_empty() && b.port != 0),
            agent_urls: file.agents,
            agents: HashMap::new(),
            agent_failure: file.agent_failure.filter(|f| f.message_json.starts_with('"')),
            dynamic_routes: file.dynamic_routes,
            cross_origin: file.cross_origin,
            error_pages: file.error_pages,
            transform_rules: file.transform_rules.and_then(|rules| rules.into_iter().map(TransformRule::ready).collect()),
            inject_request_id: file.inject_request_id,
            revision: file.revision,
            api_versions,
            client_headers: file
                .client_headers
                .unwrap_or_default()
                .into_iter()
                .filter_map(|(name, value)| Some((HeaderName::from_bytes(name.to_ascii_lowercase().as_bytes()).ok()?, HeaderValue::from_str(&value).ok()?)))
                .collect(),
            rate_limit_enabled: file.rate_limit_enabled,
            // Without these sections, the gateway's shipped defaults apply.
            circuit_breaker: file
                .circuit_breaker
                .map(|c| breaker::Config { enabled: c.enabled, threshold: c.failure_threshold, timeout_secs: c.timeout_seconds })
                .unwrap_or_default(),
            upstream_timeout: Duration::from_secs_f64(file.upstream_timeout_seconds.filter(|s| *s > 0.0 && s.is_finite()).unwrap_or(30.0)),
            keys,
            tokens,
            tenants,
            trusted_proxies,
            ip_rules,
            policies,
            fallthrough_prefixes: file.fallthrough_prefixes,
            own_paths: file.own_paths,
            protocol_urls: ROUTED.map(|p| file.backends.get(p.name()).cloned().flatten().filter(|u| !u.is_empty())),
            routes: Default::default(),
            // Without the section, the gateway's shipped defaults apply.
            boundary: file.boundary.map(Boundary::from_record).unwrap_or_else(Boundary::defaults),
        };
        Ok(Parsed::Full(snap, extras))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_query_is_read_and_written_as_the_python_gateway_does() {
        let written = |query: &str| read_query(query).map(|read| write_query(&read).unwrap_or_default());
        for (came, sent) in [
            ("a=1&b=2&a=3", "a=1&a=3&b=2"), ("flag&b=2", "flag=&b=2"), ("q=a%20b&r=c+d&s=%2F", "q=a%20b&r=c%20d&s=%2F"), ("", ""), ("&&", ""),
            ("a=1&&b", "a=1&b="), ("=x", "=x"), ("a=%zz", "a=%25zz"), ("a=b=c", "a=b%3Dc"), ("a;b=1", "a%3Bb=1"), ("caf%C3%A9=1", "caf%C3%A9=1"),
            ("a=%2541", "a=%41"), ("a=%2541+", "a=%2541%20"), ("a=%", "a=%25"), ("a=%4", "a=%254"), ("%61=%7e", "a=~"), ("a=x&A=y", "a=x&A=y"),
            ("a=1&a", "a=1&a="), ("a=%E2%82%AC", "a=%E2%82%AC"), ("k=a/b?c:d@e", "k=a%2Fb%3Fc%3Ad%40e"), ("a=%00", "a=%00"),
            ("tag=a&one=1&tag=b&tag=a", "tag=a&tag=b&tag=a&one=1"), ("a=1&%61=2", "a=1&a=2"),
        ] {
            assert_eq!(written(came).as_deref(), Some(sent), "{came}");
        }
        // Bytes that are not UTF-8 are each a replacement character, written in UTF-8.
        for (came, sent) in [("a=%ff", "a=%EF%BF%BD"), ("a=%C3", "a=%EF%BF%BD"), ("%80=1", "%EF%BF%BD=1"), ("a=%C3x", "a=%EF%BF%BDx"),
                             ("a=%E2%82", "a=%EF%BF%BD"), ("a=%ED%A0%80", "a=%EF%BF%BD%EF%BF%BD%EF%BF%BD"), ("a=%F0%9F%98", "a=%EF%BF%BD")] {
            assert_eq!(written(came).as_deref(), Some(sent), "{came}");
        }
        assert_eq!(written("caf\u{e9}=1"), None);
        // Rules read and change one value to a name. A name given more than once goes on with
        // every value unless a rule changed it; set to the value it held, it was not changed.
        let rule: Actions = serde_json::from_str(r#"{"query_remove":["a b","gone"],"query_set":[["page","1"],["new","x"],["same","2"]]}"#).unwrap();
        let mut read = read_query("gone=1&page=7&a+b=2&q=tea&gone=2&page=8&keep=x&keep=y&same=1&same=2").unwrap();
        change_query(&mut read, &[&rule]);
        assert_eq!(write_query(&read).as_deref(), Some("page=1&q=tea&keep=x&keep=y&same=1&same=2&new=x"));
    }

    #[test]
    fn rules_for_a_body_and_a_path_are_read_and_applied_in_order() {
        let snap = Snapshot::from_json(
            r#"{"keys":[],"transform_rules":[
                {"all":true,"native":true,"response":null,"request":{"body_rename":[["user","meta.user"]],"body_remove":["secret"],
                    "body_set":[["meta.source","\"gateway\""],["n","1.5"]],"path":{"from":"/old/","to":"/new/","start":true}}},
                {"glob":"/old/*","native":true,"response":null,"request":{"body_set":[["meta.user.seen","true"]],"path":{"from":"/draft","to":""}}},
                {"glob":"/odd/*","native":true,"response":null,"request":{"body_set":[["a","NaN"]]}},
                {"glob":"/answer/*","native":true,"request":null,"response":{"body_set":[["a","1"]]}},
                {"glob":"/nothing/*","native":true,"response":null,"request":{"path":{"from":"","to":"/x"}}}]}"#,
        )
        .unwrap();
        let shaping = snap.shaping("POST", "/old/draft/1/old/draft", "/old/draft/1/old/draft").unwrap();
        assert_eq!(shaping.rewritten("/old/draft/1/old/draft").unwrap().as_deref(), Some("/new/1/old"));
        let mut body = pyjson::parse(br#"{"user": "ana", "secret": 1, "keep": null}"#).unwrap();
        shaping.request.iter().for_each(|a| a.change_body(&mut body));
        let mut out = Vec::new();
        body.dump(&mut out);
        assert_eq!(String::from_utf8(out).unwrap(), r#"{"keep": null, "meta": {"user": {"seen": true}, "source": "gateway"}, "n": 1.5}"#);
        // A path only the first rule covers is rewritten by it alone.
        let shaping = snap.shaping("GET", "/other/old/", "/other/old/").unwrap();
        assert_eq!(shaping.rewritten("/other/old/").unwrap().as_deref(), Some("/other/old/"));
        // A value this side cannot be certain of and a rewrite of nothing each leave the rule
        // to the Python gateway.
        for path in ["/odd/1", "/nothing/1"] {
            assert!(snap.shaping("POST", path, path).is_none(), "{path}");
        }
        // A rule for an answer's body is this side's to apply.
        assert!(snap.shaping("POST", "/answer/1", "/answer/1").is_some());
    }

    #[test]
    fn a_path_is_rewritten_by_expression_as_python_substitutes() {
        let snap = Snapshot::from_json(
            r#"{"keys":[],"transform_rules":[
                {"glob":"/users/*","native":true,"response":null,"request":{"path":{"regex":"^/users/([0-9]+)/(posts|notes)","parts":["/", 2, "/by-user/", 1]}}},
                {"glob":"/users/*","native":true,"response":null,"request":{"path":{"regex":"/by-(user|team)","parts":["/", 0, "-", 1]}}},
                {"glob":"/twice/*","native":true,"response":null,"request":{"path":{"regex":"a(b)?","parts":["<", 1, ">"]}}},
                {"glob":"/end/*","native":true,"response":null,"request":{"path":{"regex":"$","parts":[".json"]}}},
                {"glob":"/front/*","native":true,"response":null,"request":{"path":{"from":"","to":"/base","start":true}}},
                {"glob":"/gone/*","native":true,"response":null,"request":{"path":{"regex":"/v[0-9]+","parts":[]}}},
                {"glob":"/group/*","native":true,"response":null,"request":{"path":{"regex":"/(a)","parts":[2]}}},
                {"glob":"/engine/*","native":true,"response":null,"request":{"path":{"regex":"/(?=a)","parts":[]}}}]}"#,
        )
        .unwrap();
        let rewritten = |path: &str| snap.shaping("GET", path, path).map(|s| s.rewritten(path));
        // Each rule works on what the last left; a group is put back by its number, 0 the match.
        assert_eq!(rewritten("/users/42/posts/7"), Some(Ok(Some("/posts//by-user-user/42/7".to_owned()))));
        assert_eq!(rewritten("/users/x/posts"), Some(Ok(Some("/users/x/posts".to_owned()))));
        // Every match from the left is replaced; a group that took no part leaves nothing.
        assert_eq!(rewritten("/twice/ab/a"), Some(Ok(Some("/twice/<b>/<>".to_owned()))));
        assert_eq!(rewritten("/end/x"), Some(Ok(Some("/end/x.json".to_owned()))));
        assert_eq!(rewritten("/front/x"), Some(Ok(Some("/base/front/x".to_owned()))));
        assert_eq!(rewritten("/gone/v2/x/v10"), Some(Ok(Some("/gone/x".to_owned()))));
        // Beyond ASCII the two engines' classes differ: the Python gateway rewrites that path.
        assert_eq!(rewritten("/gone/caf\u{e9}"), Some(Err(())));
        assert_eq!(rewritten("/front/caf\u{e9}"), Some(Ok(Some("/base/front/caf\u{e9}".to_owned()))));
        // A group the expression does not have, and an expression this engine does not take.
        assert_eq!(rewritten("/group/a"), None);
        assert_eq!(rewritten("/engine/a"), None);
    }

    #[test]
    fn a_pattern_that_is_an_expression_is_pythons_to_match_beyond_ascii() {
        let snap = Snapshot::from_json(
            r#"{"keys":[],"transform_rules":[{"regex":"/w/\\w+$","native":true,"response":null,"request":{"set":[["x-a","1"]]}},
                {"glob":"/g/*","native":true,"response":null,"request":{"set":[["x-a","1"]]}}]}"#,
        )
        .unwrap();
        assert_eq!(snap.shaping("GET", "/w/abc", "/w/abc").map(|s| s.request.len()), Some(1));
        assert_eq!(snap.shaping("GET", "/other", "/other").map(|s| s.request.len()), Some(0));
        assert!(snap.shaping("GET", "/w/\u{b2}", "/w/\u{b2}").is_none());
        assert!(snap.transformed("GET", "/other/\u{b2}", "/other/\u{b2}"));
        assert!(!snap.transformed("GET", "/other", "/other"));
    }

    #[test]
    fn hex_round_trips_a_digest() {
        let d = hash_key(b"sk-test");
        let hex: String = d.iter().map(|b| format!("{b:02x}")).collect();
        assert_eq!(parse_hex32(&hex), Some(d));
        assert_eq!(parse_hex32("zz"), None);
    }

    #[test]
    fn the_key_header_and_the_refusal_wording_are_read() {
        let snap = Snapshot::from_json(r#"{"keys":[]}"#).unwrap();
        assert!(snap.key_header == "x-api-key" && snap.refusals.is_none());
        let snap = Snapshot::from_json(
            r#"{"keys":[],"key_header":"X-Gateway-Key","refusals":{
                "missing_key":{"message_json":"\"m\"","details_json":"{}"},"invalid_key":{"message_json":"\"i\"","details_json":"{\"a\":1}"}}}"#,
        )
        .unwrap();
        assert_eq!(snap.key_header, "x-gateway-key");
        assert_eq!(snap.refusals.unwrap().invalid_key.details_json, r#"{"a":1}"#);
        assert!(Snapshot::from_json(r#"{"keys":[],"key_header":"not a header"}"#).is_err());
        assert!(Snapshot::from_json(r#"{"keys":[],"refusals":{"missing_key":{"message_json":"m","details_json":"{}"},"invalid_key":{"message_json":"\"i\"","details_json":"{}"}}}"#).is_err());
    }

    #[test]
    fn loads_keys_and_defaults() {
        let d = hash_key(b"sk-test");
        let hex: String = d.iter().map(|b| format!("{b:02x}")).collect();
        let snap = Snapshot::from_json(&format!(
            r#"{{"keys":[{{"hash":"{hex}","id":"k1","rate_limit_per_minute":5,"rate_limit_per_hour":50}}]}}"#
        ))
        .unwrap();
        let e = snap.keys.get(&d).unwrap();
        assert!(e.is_active && e.native_ok && e.per_minute == 5);
    }

    #[test]
    fn shipped_version_defaults_apply_when_the_section_is_absent() {
        let snap = Snapshot::from_json(r#"{"keys":[]}"#).unwrap();
        assert_eq!(snap.api_versions.default.as_deref(), Some("1"));
        assert_eq!(snap.api_versions.native_ok.get("2"), Some(&true));
    }

    #[test]
    fn the_health_path_and_readiness_are_read_and_checked() {
        let snap = Snapshot::from_json(r#"{"keys":[]}"#).unwrap();
        assert!(snap.ops.is_none());
        let text = r#"{"keys":[],"ops":{"health_path":"/health","version_json":"\"1.2\""},"ready":{"status":200,"body_json":"{\"status\":\"ready\"}"}}"#;
        let Parsed::Full(snap, extras) = Snapshot::parse(text, None).unwrap() else { panic!("expected a full snapshot") };
        let ops = snap.ops.unwrap();
        assert_eq!((ops.health_path.as_deref(), ops.version_json.as_str()), (Some("/health"), "\"1.2\""));
        assert_eq!(extras.ready, Some(Readiness { status: 200, body_json: r#"{"status":"ready"}"#.into() }));
        // Readiness comes with an answer that says nothing else changed, too.
        let same = r#"{"unchanged":true,"revision":"r","ready":{"status":503,"body_json":"{}"}}"#;
        let Parsed::Unchanged(extras) = Snapshot::parse(same, Some(&snap_with_nothing())).unwrap() else { panic!("expected unchanged") };
        assert_eq!(extras.ready.unwrap().status, 503);
        // Neither is taken on trust: both are put into answers as they are.
        for bad in [
            r#"{"keys":[],"ops":{"health_path":"/health","version_json":"1.2"}}"#,
            r#"{"keys":[],"ready":{"status":204,"body_json":"{}"}}"#,
            r#"{"keys":[],"ready":{"status":200,"body_json":"ready"}}"#,
        ] {
            assert!(Snapshot::from_json(bad).is_err(), "{bad}");
        }
    }

    fn snap_with_nothing() -> Snapshot {
        Snapshot::from_json(r#"{"keys":[]}"#).unwrap()
    }

    #[test]
    fn counters_survive_an_update_and_unchanged_is_recognised() {
        let d = hash_key(b"sk-test");
        let hex: String = d.iter().map(|b| format!("{b:02x}")).collect();
        let text = format!(
            r#"{{"revision":"r1","keys":[{{"hash":"{hex}","id":"k1","rate_limit_per_minute":2,"rate_limit_per_hour":50}}]}}"#
        );
        let first = Snapshot::from_json(&text).unwrap();
        let e = first.keys.get(&d).unwrap();
        assert!(e.minute.allow(2, 60, 1000) && e.minute.allow(2, 60, 1000));
        let Parsed::Full(second, _) = Snapshot::parse(&text, Some(&first)).unwrap() else { panic!("expected a full snapshot") };
        assert!(!second.keys.get(&d).unwrap().minute.allow(2, 60, 1000), "the window was reset by the update");
        assert_eq!(second.revision.as_deref(), Some("r1"));
        assert!(matches!(
            Snapshot::parse(r#"{"unchanged":true,"revision":"r1"}"#, Some(&first)).unwrap(),
            Parsed::Unchanged(Extras { usage: None, ready: None })
        ));
    }

    #[test]
    fn breaker_and_timeout_come_from_the_control_plane_or_default() {
        let snap = Snapshot::from_json(r#"{"keys":[]}"#).unwrap();
        assert_eq!(snap.circuit_breaker, breaker::Config { enabled: true, threshold: 5, timeout_secs: 60 });
        assert_eq!(snap.upstream_timeout, Duration::from_secs(30));
        let snap = Snapshot::from_json(
            r#"{"keys":[],"upstream_timeout_seconds":2.5,"circuit_breaker":{"enabled":false,"failure_threshold":2,"timeout_seconds":7}}"#,
        )
        .unwrap();
        assert_eq!(snap.circuit_breaker, breaker::Config { enabled: false, threshold: 2, timeout_secs: 7 });
        assert_eq!(snap.upstream_timeout, Duration::from_millis(2500));
    }

    fn hex(key: &[u8]) -> String {
        hash_key(key).iter().map(|b| format!("{b:02x}")).collect()
    }

    fn governed() -> String {
        format!(
            r#"{{"revision":"r1","keys":[
              {{"hash":"{}","id":"k1","rate_limit_per_minute":5,"rate_limit_per_hour":50,"tenant":"acme",
                "allowed_paths":{{"rules":[["under","/api/users"],["prefix","/api/pub"]],"patterns_json":"[\"/api/users\",\"/api/pub*\"]"}}}}],
             "tenants":{{"acme":{{"minute":2,"hour":null,"details_json":"{{\"tenant_id\":\"acme\",\"quota\":{{}}}}"}}}},
             "trusted_proxies":["127.0.0.1/32"],
             "ip_rules":[{{"scope":"tenant","scope_id":"acme","allow":false,"net":"10.0.0.0/8","details_json":"{{\"rule_id\":\"r\",\"scope\":\"tenant\"}}"}}],
             "policies":[{{"path":"/api/admin*","methods":["DELETE"],"tenants":[],"cidrs":null,"time":[60,120],"deny":true,
                          "message_json":"\"no\"","details_json":"{{\"policy_id\":\"p\"}}"}}],
             "tenant_usage":{{"minute_start":600,"hour_start":0,"used":{{"acme":[2,2]}}}}}}"#,
            hex(b"sk-test")
        )
    }

    #[test]
    fn tenants_scopes_rules_and_policies_are_loaded() {
        let snap = Snapshot::from_json(&governed()).unwrap();
        let e = snap.keys.get(&hash_key(b"sk-test")).unwrap();
        let tenant = e.tenant.as_ref().unwrap();
        assert_eq!((&*tenant.id, tenant.minute, tenant.hour), ("acme", Some(2), None));
        let scope = e.scope.as_ref().unwrap();
        assert!(scope.allows("/api/users/7") && scope.allows("/api/public") && !scope.allows("/api/orders"));
        assert!(snap.ip_rules.blocking("10.1.2.3".parse().unwrap(), Some("acme"), Some("k1")).is_some());
        assert!(snap.ip_rules.blocking("10.1.2.3".parse().unwrap(), None, Some("k1")).is_none());
        assert_eq!(snap.policies.len(), 1);
        assert_eq!(snap.trusted_proxies.len(), 1);
        // The database already holds the tenant's two requests for the minute starting at 600.
        assert!(tenant.usage.admit(tenant.minute, tenant.hour, 610).is_err());
        assert!(tenant.usage.admit(tenant.minute, tenant.hour, 660).is_ok());
    }

    #[test]
    fn a_tenants_counts_survive_an_update_and_an_unchanged_answer_carries_usage() {
        let first = Snapshot::from_json(&governed()).unwrap();
        let Parsed::Full(second, Extras { usage, .. }) = Snapshot::parse(&governed(), Some(&first)).unwrap() else { panic!("expected a full snapshot") };
        assert!(Arc::ptr_eq(&first.tenants["acme"].usage, &second.tenants["acme"].usage));
        assert!(usage.is_some());
        let answer = r#"{"unchanged":true,"revision":"r1","tenant_usage":{"minute_start":660,"hour_start":0,"used":{}}}"#;
        let Parsed::Unchanged(Extras { usage: Some(usage), .. }) = Snapshot::parse(answer, Some(&second)).unwrap() else { panic!("expected usage") };
        usage.apply(&second);
        let t = &second.tenants["acme"];
        assert!(t.usage.admit(t.minute, t.hour, 661).is_ok());
    }

    #[test]
    fn what_cannot_be_read_is_refused_whole() {
        let bad_net = r#"{"keys":[],"trusted_proxies":["nonsense"]}"#;
        assert!(Snapshot::from_json(bad_net).is_err());
        let unknown_tenant = format!(r#"{{"keys":[{{"hash":"{}","id":"k","rate_limit_per_minute":1,"rate_limit_per_hour":1,"tenant":"ghost"}}]}}"#, hex(b"k"));
        assert!(Snapshot::from_json(&unknown_tenant).is_err());
        let odd_rule = format!(
            r#"{{"keys":[{{"hash":"{}","id":"k","rate_limit_per_minute":1,"rate_limit_per_hour":1,"allowed_paths":{{"rules":[["regex","x"]],"patterns_json":"[]"}}}}]}}"#,
            hex(b"k")
        );
        assert!(Snapshot::from_json(&odd_rule).is_err());
    }

    #[test]
    fn a_bad_hash_is_rejected() {
        assert!(Snapshot::from_json(r#"{"keys":[{"hash":"abc","id":"k","rate_limit_per_minute":1,"rate_limit_per_hour":1}]}"#).is_err());
    }
}
