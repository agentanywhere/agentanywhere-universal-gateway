//! Who may call what: the client address, IP rules, policies and a key's allowed paths.
//!
//! Each function here reproduces one decision of the Python gateway (`app/client_ip.py`,
//! `app/governance.py`, `path_is_allowed` in `app/middleware/auth.py`) from what the control plane
//! compiled into the snapshot. Where the two could read an input differently (an address the
//! standard libraries format differently, a header that is not plain text) the answer is "not
//! sure", and the request goes to the Python gateway instead of being decided here.
use hyper::HeaderMap;
use std::collections::HashMap;
use std::net::{IpAddr, SocketAddr};

/// A network in canonical text (`10.0.0.0/8`, `::1/128`), as Python's `ipaddress` prints it.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Net {
    addr: IpAddr,
    prefix: u8,
}

impl Net {
    pub fn parse(text: &str) -> Option<Net> {
        let (addr, prefix) = match text.split_once('/') {
            Some((a, p)) => (a, Some(p)),
            None => (text, None),
        };
        let addr: IpAddr = addr.parse().ok()?;
        let max = if addr.is_ipv4() { 32 } else { 128 };
        let prefix = match prefix {
            Some(p) => p.parse::<u8>().ok().filter(|n| *n <= max)?,
            None => max,
        };
        Some(Net { addr, prefix })
    }

    /// An address of the other family is never inside, as in Python.
    pub fn contains(&self, ip: IpAddr) -> bool {
        match (self.addr, ip) {
            (IpAddr::V4(n), IpAddr::V4(i)) => {
                let shift = 32 - u32::from(self.prefix);
                shift == 32 || (u32::from(n) >> shift) == (u32::from(i) >> shift)
            }
            (IpAddr::V6(n), IpAddr::V6(i)) => {
                let shift = 128 - u32::from(self.prefix);
                shift == 128 || (u128::from(n) >> shift) == (u128::from(i) >> shift)
            }
            _ => false,
        }
    }
}

fn trusted(ip: IpAddr, proxies: &[Net]) -> bool {
    proxies.iter().any(|n| n.contains(ip))
}

/// One `X-Forwarded-For` entry as `_parse_ip` reads it. `None` when it is not an address this
/// side is sure to read the same way.
fn parse_hop(hop: &str) -> Option<IpAddr> {
    let mut v = hop.trim().trim_matches('"');
    if let Some(rest) = v.strip_prefix('[') {
        v = rest.split(']').next().unwrap_or("");
    } else if v.bytes().filter(|b| *b == b':').count() == 1 {
        v = v.split(':').next().unwrap_or("");
    }
    sure(v.parse().ok()?)
}

/// `None` for the IPv6 forms Python prints differently from one version to the next
/// (`::ffff:1.2.3.4` and its neighbours).
fn sure(ip: IpAddr) -> Option<IpAddr> {
    if let IpAddr::V6(v6) = ip {
        let s = v6.segments();
        if s[..5].iter().all(|x| *x == 0) && !v6.is_unspecified() && !v6.is_loopback() {
            return None;
        }
    }
    Some(ip)
}

/// The client address, as `resolve_client_ip` decides it: the socket peer, unless the peer is a
/// trusted proxy, in which case the right-most `X-Forwarded-For` entry that is not one. `None`
/// when this side cannot be sure of reading the header as the Python gateway does.
pub fn client_ip(peer: SocketAddr, headers: &HeaderMap, proxies: &[Net]) -> Option<IpAddr> {
    if let SocketAddr::V6(v6) = peer {
        if v6.scope_id() != 0 {
            return None;
        }
    }
    let peer = sure(peer.ip())?;
    if proxies.is_empty() {
        return Some(peer);
    }
    let mut values = headers.get_all("x-forwarded-for").iter();
    let Some(first) = values.next() else { return Some(peer) };
    if values.next().is_some() {
        return None;
    }
    let text = first.to_str().ok()?;
    if text.is_empty() || !trusted(peer, proxies) {
        return Some(peer);
    }
    let mut leftmost = None;
    for hop in text.split(',').rev().map(str::trim).filter(|h| !h.is_empty()) {
        let ip = parse_hop(hop)?;
        if !trusted(ip, proxies) {
            return Some(ip);
        }
        leftmost = Some(ip);
    }
    // Every hop is a trusted proxy: the left-most one is the closest we get.
    Some(leftmost.unwrap_or(peer))
}

pub struct IpRule {
    pub allow: bool,
    /// `None` for a CIDR that does not parse: it never matches, but an allow rule still counts
    /// as "this scope has an allow list".
    pub net: Option<Net>,
    /// `{"rule_id":...,"scope":...}`: what a refusal by this rule says after the client address.
    pub details_json: String,
}

/// The rules of one scope, in the order the Python engine indexes them.
#[derive(Default)]
pub struct Bucket {
    rules: Vec<IpRule>,
}

impl Bucket {
    /// The rule that blocks `ip`: the first deny rule it is in, else, when the scope has allow
    /// rules and `ip` is in none of them, the first allow rule.
    fn blocking(&self, ip: IpAddr) -> Option<&IpRule> {
        if let Some(r) = self.rules.iter().find(|r| !r.allow && r.net.is_some_and(|n| n.contains(ip))) {
            return Some(r);
        }
        let mut allows = self.rules.iter().filter(|r| r.allow);
        let first = allows.next()?;
        if first.net.is_some_and(|n| n.contains(ip)) || allows.any(|r| r.net.is_some_and(|n| n.contains(ip))) {
            return None;
        }
        Some(first)
    }
}

#[derive(Default)]
pub struct IpRules {
    global: Bucket,
    tenant: HashMap<String, Bucket>,
    key: HashMap<String, Bucket>,
}

impl IpRules {
    pub fn add(&mut self, scope: &str, scope_id: Option<String>, rule: IpRule) -> Result<(), String> {
        let bucket = match (scope, scope_id) {
            ("global", _) => &mut self.global,
            ("tenant", Some(id)) => self.tenant.entry(id).or_default(),
            ("api_key", Some(id)) => self.key.entry(id).or_default(),
            (other, _) => return Err(format!("IP rule with scope {other} and no id")),
        };
        bucket.rules.push(rule);
        Ok(())
    }

    /// Global rules, then the tenant's, then the key's; the first scope that blocks decides.
    pub fn blocking(&self, ip: IpAddr, tenant: Option<&str>, key_id: &str) -> Option<&IpRule> {
        if let Some(r) = self.global.blocking(ip) {
            return Some(r);
        }
        if let Some(r) = tenant.and_then(|t| self.tenant.get(t)).and_then(|b| b.blocking(ip)) {
            return Some(r);
        }
        self.key.get(key_id).and_then(|b| b.blocking(ip))
    }
}

/// `fnmatch` for patterns without character classes (the control plane keeps those): `*` is any
/// run of characters, `/` included, and `?` is exactly one. Both sides are ASCII here.
pub fn fnmatch(pattern: &[u8], text: &[u8]) -> bool {
    let (mut p, mut t) = (0, 0);
    let mut star: Option<(usize, usize)> = None;
    while t < text.len() {
        if p < pattern.len() && (pattern[p] == b'?' || (pattern[p] != b'*' && pattern[p] == text[t])) {
            p += 1;
            t += 1;
        } else if p < pattern.len() && pattern[p] == b'*' {
            star = Some((p, t));
            p += 1;
        } else if let Some((sp, st)) = star {
            p = sp + 1;
            t = st + 1;
            star = Some((sp, st + 1));
        } else {
            return false;
        }
    }
    pattern[p..].iter().all(|c| *c == b'*')
}

pub struct Policy {
    pub path: Option<String>,
    pub methods: Vec<String>,
    pub tenants: Vec<String>,
    /// `None`: any address. A list: the address must be in one of them.
    pub cidrs: Option<Vec<Net>>,
    /// Start and end of the daily window, in minutes after midnight UTC.
    pub time: Option<(u32, u32)>,
    pub deny: bool,
    /// The refusal message, already a JSON string.
    pub message_json: String,
    /// `{"policy_id":...}`.
    pub details_json: String,
}

/// What a policy is matched against. `micros_of_day` is the UTC time of day.
pub struct Subject<'a> {
    pub path: &'a str,
    pub method: &'a str,
    pub tenant: &'a str,
    pub ip: IpAddr,
    pub micros_of_day: u64,
}

impl Policy {
    fn matches(&self, s: &Subject) -> bool {
        if let Some(pattern) = &self.path {
            if !fnmatch(pattern.as_bytes(), s.path.as_bytes()) {
                return false;
            }
        }
        if !self.methods.is_empty() && !self.methods.iter().any(|m| m == s.method) {
            return false;
        }
        if !self.tenants.is_empty() && !self.tenants.iter().any(|t| t == s.tenant) {
            return false;
        }
        if let Some(nets) = &self.cidrs {
            if !nets.iter().any(|n| n.contains(s.ip)) {
                return false;
            }
        }
        if let Some((start, end)) = self.time {
            let (start, end) = (u64::from(start) * 60_000_000, u64::from(end) * 60_000_000);
            let inside = if start <= end {
                start <= s.micros_of_day && s.micros_of_day <= end
            } else {
                s.micros_of_day >= start || s.micros_of_day <= end
            };
            if !inside {
                return false;
            }
        }
        true
    }
}

/// The first policy that matches decides. Returns it when it refuses the request.
pub fn refusing<'a>(policies: &'a [Policy], s: &Subject) -> Option<&'a Policy> {
    policies.iter().find(|p| p.matches(s)).filter(|p| p.deny)
}

/// `_normalise_scope_path`: empty segments collapse; a `.` or `..` segment has no normal form.
pub fn normalise(path: &str) -> Option<String> {
    let mut out = String::with_capacity(path.len() + 1);
    for seg in path.split('/').filter(|s| !s.is_empty()) {
        if seg == "." || seg == ".." {
            return None;
        }
        out.push('/');
        out.push_str(seg);
    }
    if out.is_empty() {
        out.push('/');
    }
    Some(out)
}

pub enum ScopeRule {
    All,
    /// The normalised path starts with this text.
    Prefix(String),
    /// The normalised path is this, or below it.
    Under(String),
}

/// A key's `allowed_paths`, compiled by the control plane.
pub struct Scope {
    pub rules: Vec<ScopeRule>,
    /// The patterns as the key holds them, as JSON, for the refusal body.
    pub patterns_json: String,
}

impl Scope {
    pub fn allows(&self, path: &str) -> bool {
        let Some(normal) = normalise(if path.is_empty() { "/" } else { path }) else { return false };
        self.rules.iter().any(|r| match r {
            ScopeRule::All => true,
            ScopeRule::Prefix(p) => normal.starts_with(p.as_str()),
            ScopeRule::Under(base) => {
                normal == *base || (normal.starts_with(base.as_str()) && normal.as_bytes().get(base.len()) == Some(&b'/'))
            }
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use hyper::header::HeaderValue;

    fn net(s: &str) -> Net {
        Net::parse(s).unwrap()
    }

    fn ip(s: &str) -> IpAddr {
        s.parse().unwrap()
    }

    #[test]
    fn networks_contain_their_own_family_only() {
        assert!(net("10.0.0.0/8").contains(ip("10.9.8.7")));
        assert!(!net("10.0.0.0/8").contains(ip("11.0.0.1")));
        assert!(net("0.0.0.0/0").contains(ip("203.0.113.5")));
        assert!(net("203.0.113.5").contains(ip("203.0.113.5")));
        assert!(net("2001:db8::/32").contains(ip("2001:db8::1")));
        assert!(net("::/0").contains(ip("::1")));
        assert!(!net("0.0.0.0/0").contains(ip("::ffff:10.0.0.1")));
        assert!(!net("::/0").contains(ip("10.0.0.1")));
        assert!(Net::parse("10.0.0.0/33").is_none());
        assert!(Net::parse("example.com").is_none());
    }

    fn resolve(peer: &str, xff: Option<&str>, proxies: &[&str]) -> Option<String> {
        let mut h = HeaderMap::new();
        if let Some(x) = xff {
            h.insert("x-forwarded-for", HeaderValue::from_str(x).unwrap());
        }
        let nets: Vec<Net> = proxies.iter().map(|p| net(p)).collect();
        client_ip(format!("{peer}:4000").parse().unwrap(), &h, &nets).map(|i| i.to_string())
    }

    #[test]
    fn the_header_counts_only_from_a_trusted_proxy() {
        assert_eq!(resolve("198.51.100.7", Some("1.2.3.4"), &[]).as_deref(), Some("198.51.100.7"));
        assert_eq!(resolve("198.51.100.7", Some("1.2.3.4"), &["10.0.0.0/8"]).as_deref(), Some("198.51.100.7"));
        assert_eq!(resolve("10.0.0.1", None, &["10.0.0.0/8"]).as_deref(), Some("10.0.0.1"));
        assert_eq!(resolve("10.0.0.1", Some(""), &["10.0.0.0/8"]).as_deref(), Some("10.0.0.1"));
    }

    #[test]
    fn the_right_most_untrusted_hop_is_the_client() {
        let p = ["10.0.0.0/8"];
        assert_eq!(resolve("10.0.0.1", Some("9.9.9.9, 203.0.113.5, 10.0.0.2"), &p).as_deref(), Some("203.0.113.5"));
        assert_eq!(resolve("10.0.0.1", Some("203.0.113.5:8080"), &p).as_deref(), Some("203.0.113.5"));
        assert_eq!(resolve("10.0.0.1", Some("\"[2001:db8::1]:443\""), &p).as_deref(), Some("2001:db8::1"));
        // Every hop trusted: the left-most.
        assert_eq!(resolve("10.0.0.1", Some("10.0.0.3, 10.0.0.2"), &p).as_deref(), Some("10.0.0.3"));
        assert_eq!(resolve("10.0.0.1", Some(" , "), &p).as_deref(), Some("10.0.0.1"));
    }

    #[test]
    fn anything_doubtful_is_left_to_the_control_plane() {
        let p = ["10.0.0.0/8"];
        assert_eq!(resolve("10.0.0.1", Some("not-an-address"), &p), None);
        assert_eq!(resolve("10.0.0.1", Some("fe80::1%eth0"), &p), None);
        assert_eq!(resolve("10.0.0.1", Some("::ffff:1.2.3.4"), &p), None);
        // Garbage to the left of the client is never read.
        assert_eq!(resolve("10.0.0.1", Some("garbage, 203.0.113.5"), &p).as_deref(), Some("203.0.113.5"));
        let mut h = HeaderMap::new();
        h.append("x-forwarded-for", HeaderValue::from_static("1.1.1.1"));
        h.append("x-forwarded-for", HeaderValue::from_static("2.2.2.2"));
        assert_eq!(client_ip("10.0.0.1:1".parse().unwrap(), &h, &[net("10.0.0.0/8")]), None);
    }

    fn rule(allow: bool, cidr: &str, id: &str) -> IpRule {
        IpRule { allow, net: Net::parse(cidr), details_json: format!("{{\"rule_id\":\"{id}\"}}") }
    }

    #[test]
    fn deny_rules_win_and_an_allow_list_excludes_everyone_else() {
        let mut rules = IpRules::default();
        rules.add("global", None, rule(true, "10.0.0.0/8", "a1")).unwrap();
        rules.add("global", None, rule(true, "192.168.0.0/16", "a2")).unwrap();
        rules.add("global", None, rule(false, "10.6.6.0/24", "d1")).unwrap();
        let hit = |addr: &str| rules.blocking(ip(addr), None, "k").map(|r| r.details_json.clone());
        assert_eq!(hit("10.1.1.1"), None);
        assert_eq!(hit("192.168.4.4"), None);
        assert_eq!(hit("10.6.6.6").as_deref(), Some("{\"rule_id\":\"d1\"}"));
        assert_eq!(hit("8.8.8.8").as_deref(), Some("{\"rule_id\":\"a1\"}"), "blocked by the first allow rule");
    }

    #[test]
    fn scopes_are_checked_global_then_tenant_then_key() {
        let mut rules = IpRules::default();
        rules.add("tenant", Some("t1".into()), rule(false, "10.0.0.0/8", "t")).unwrap();
        rules.add("api_key", Some("k1".into()), rule(false, "0.0.0.0/0", "k")).unwrap();
        let hit = |tenant: Option<&str>, key: &str| rules.blocking(ip("10.1.1.1"), tenant, key).map(|r| r.details_json.clone());
        assert_eq!(hit(None, "k2"), None);
        assert_eq!(hit(Some("t1"), "k1").as_deref(), Some("{\"rule_id\":\"t\"}"));
        assert_eq!(hit(Some("t2"), "k1").as_deref(), Some("{\"rule_id\":\"k\"}"));
        assert!(rules.add("tenant", None, rule(false, "10.0.0.0/8", "x")).is_err());
    }

    #[test]
    fn an_unreadable_allow_rule_still_makes_an_allow_list() {
        let mut rules = IpRules::default();
        rules.add("global", None, rule(true, "nonsense", "a")).unwrap();
        assert!(rules.blocking(ip("10.1.1.1"), None, "k").is_some());
    }

    #[test]
    fn wildcards_match_like_fnmatch() {
        for (pattern, text, want) in [
            ("/api/*", "/api/users/7", true),
            ("/api/*", "/api/", true),
            ("/api/*", "/api", false),
            ("/api/*/orders", "/api/a/b/orders", true),
            ("/api/user?", "/api/users", true),
            ("/api/user?", "/api/user", false),
            ("*", "", true),
            ("", "", true),
            ("", "x", false),
            ("/api/admin", "/api/admin", true),
            ("/api/admin", "/api/Admin", false),
            ("*admin*", "/api/x/admin/y", true),
            ("/a*b*c", "/a-b-b-c", true),
            ("/a*b*c", "/a-b-b-", false),
        ] {
            assert_eq!(fnmatch(pattern.as_bytes(), text.as_bytes()), want, "{pattern} against {text}");
        }
    }

    fn policy(deny: bool) -> Policy {
        Policy {
            path: None,
            methods: vec![],
            tenants: vec![],
            cidrs: None,
            time: None,
            deny,
            message_json: "\"no\"".into(),
            details_json: "{}".into(),
        }
    }

    fn subject<'a>(path: &'a str, method: &'a str) -> Subject<'a> {
        Subject { path, method, tenant: "", ip: ip("203.0.113.5"), micros_of_day: 12 * 3_600_000_000 }
    }

    #[test]
    fn the_first_matching_policy_decides() {
        let allow_get = Policy { methods: vec!["GET".into()], path: Some("/api/open/*".into()), ..policy(false) };
        let deny_all = policy(true);
        let policies = [allow_get, deny_all];
        assert!(refusing(&policies, &subject("/api/open/x", "GET")).is_none());
        assert!(refusing(&policies, &subject("/api/open/x", "POST")).is_some());
        assert!(refusing(&policies, &subject("/api/shut/x", "GET")).is_some());
        assert!(refusing(&[], &subject("/api/x", "GET")).is_none());
    }

    #[test]
    fn conditions_narrow_a_policy() {
        let s = subject("/api/x", "GET");
        assert!(Policy { tenants: vec!["t1".into()], ..policy(true) }.matches(&Subject { tenant: "t1", ..subject("/api/x", "GET") }));
        assert!(!Policy { tenants: vec!["t1".into()], ..policy(true) }.matches(&s));
        assert!(Policy { cidrs: Some(vec![net("203.0.113.0/24")]), ..policy(true) }.matches(&s));
        assert!(!Policy { cidrs: Some(vec![net("10.0.0.0/8")]), ..policy(true) }.matches(&s));
        assert!(!Policy { cidrs: Some(vec![]), ..policy(true) }.matches(&s), "only unreadable CIDRs: never matches");
    }

    #[test]
    fn time_windows_include_their_ends_and_may_cross_midnight() {
        let at = |h: u64, m: u64, extra: u64| Subject { micros_of_day: (h * 60 + m) * 60_000_000 + extra, ..subject("/api/x", "GET") };
        let day = Policy { time: Some((9 * 60, 17 * 60)), ..policy(true) };
        assert!(day.matches(&at(9, 0, 0)));
        assert!(day.matches(&at(17, 0, 0)));
        assert!(!day.matches(&at(17, 0, 1)), "one microsecond past the end is outside");
        assert!(!day.matches(&at(8, 59, 59_999_999)));
        let night = Policy { time: Some((22 * 60, 6 * 60)), ..policy(true) };
        assert!(night.matches(&at(23, 30, 0)));
        assert!(night.matches(&at(5, 59, 0)));
        assert!(!night.matches(&at(12, 0, 0)));
    }

    #[test]
    fn paths_normalise_like_the_gateway() {
        assert_eq!(normalise("/api//users/").as_deref(), Some("/api/users"));
        assert_eq!(normalise("/").as_deref(), Some("/"));
        assert_eq!(normalise("/api/../admin"), None);
        assert_eq!(normalise("/api/./x"), None);
        assert_eq!(normalise("/api/.../x").as_deref(), Some("/api/.../x"));
    }

    #[test]
    fn a_scope_matches_whole_segments_or_a_plain_prefix() {
        let scope = |rules: Vec<ScopeRule>| Scope { rules, patterns_json: "[]".into() };
        let under = scope(vec![ScopeRule::Under("/api/users".into())]);
        assert!(under.allows("/api/users"));
        assert!(under.allows("/api/users//7"));
        assert!(!under.allows("/api/usersx"));
        assert!(!under.allows("/api/users/../admin"));
        let prefix = scope(vec![ScopeRule::Prefix("/api/v".into())]);
        assert!(prefix.allows("/api/v1/x"));
        assert!(!prefix.allows("/api/x"));
        assert!(scope(vec![ScopeRule::All]).allows("/api/anything"));
        assert!(!scope(vec![ScopeRule::All]).allows("/api/../x"), "dot segments are refused for any scoped key");
        assert!(!scope(vec![]).allows("/api/users"), "no usable pattern allows nothing");
    }
}
