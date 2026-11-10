//! One line per request served natively, in the Python gateway's access-log schema.
//!
//! Off unless asked for: the control plane already receives every request as metrics and a
//! sample as request-log rows. When on, lines are handed to a writer thread through a bounded
//! queue, so a slow or blocked stdout drops log lines (and says how many) rather than slowing
//! requests down.
use std::io::Write;
use std::net::IpAddr;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc::{sync_channel, SyncSender};
use std::sync::Arc;
use std::time::Duration;

use crate::proxy::timestamp;

const QUEUE: usize = 65_536;

pub struct AccessLog {
    tx: SyncSender<String>,
    dropped: Arc<AtomicU64>,
}

impl AccessLog {
    pub fn start() -> AccessLog {
        let (tx, rx) = sync_channel::<String>(QUEUE);
        let dropped = Arc::new(AtomicU64::new(0));
        let lost = dropped.clone();
        let spawned = std::thread::Builder::new().name("access-log".into()).spawn(move || {
            let mut out = std::io::BufWriter::with_capacity(1 << 16, std::io::stdout());
            while let Ok(first) = rx.recv() {
                let _ = out.write_all(first.as_bytes());
                while let Ok(next) = rx.try_recv() {
                    let _ = out.write_all(next.as_bytes());
                }
                let _ = out.flush();
                let n = lost.swap(0, Ordering::Relaxed);
                if n > 0 {
                    eprintln!("dataplane: {n} access-log lines dropped (stdout is not keeping up)");
                }
            }
        });
        if let Err(e) = spawned {
            eprintln!("dataplane: access log disabled: {e}");
        }
        AccessLog { tx, dropped }
    }

    pub fn write(&self, line: String) {
        if self.tx.try_send(line).is_err() {
            self.dropped.fetch_add(1, Ordering::Relaxed);
        }
    }
}

pub struct Entry<'a> {
    pub at: Duration,
    pub request_id: &'a str,
    pub traceparent: &'a str,
    pub key_id: &'a str,
    pub tenant: Option<&'a str>,
    pub ip: IpAddr,
    pub method: &'a str,
    pub path: &'a str,
    pub query: &'a str,
    pub status: u16,
    pub bytes: u64,
    pub seconds: f64,
    pub user_agent: Option<&'a str>,
    pub referrer: Option<&'a str>,
}

fn text(out: &mut String, key: &str, value: Option<&str>) {
    out.push_str(",\"");
    out.push_str(key);
    out.push_str("\":");
    match value.map(serde_json::to_string) {
        Some(Ok(v)) => out.push_str(&v),
        _ => out.push_str("null"),
    }
}

/// The line for one request, ending in a newline.
pub fn line(e: &Entry) -> String {
    let mut out = String::with_capacity(512);
    out.push_str("{\"@timestamp\":\"");
    out.push_str(&timestamp(e.at, true));
    out.push_str("\",\"level\":\"info\",\"logger\":\"dataplane\",\"event\":\"http_access\",\"message\":\"http_access\"");
    out.push_str(",\"service.name\":\"agentanywhere-dataplane\",\"service.version\":\"");
    out.push_str(env!("CARGO_PKG_VERSION"));
    out.push('"');
    text(&mut out, "request_id", Some(e.request_id));
    // `00-<trace id>-<span id>-01`
    text(&mut out, "trace.id", e.traceparent.get(3..35));
    text(&mut out, "span.id", e.traceparent.get(36..52));
    text(&mut out, "tenant_id", e.tenant);
    text(&mut out, "api_key_id", Some(e.key_id));
    out.push_str(&format!(",\"client.ip\":\"{}\"", e.ip));
    text(&mut out, "http.request.method", Some(e.method));
    text(&mut out, "http.request.path", Some(e.path));
    text(&mut out, "http.request.query", Some(e.query));
    out.push_str(&format!(
        ",\"http.response.status_code\":{},\"http.response.bytes\":{},\"http.duration_ms\":{:.3}",
        e.status,
        e.bytes,
        e.seconds * 1000.0
    ));
    text(&mut out, "user_agent.original", e.user_agent);
    text(&mut out, "http.referrer", e.referrer);
    out.push_str("}\n");
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_line_is_json_with_the_python_field_names() {
        let l = line(&Entry {
            at: Duration::new(1_700_000_000, 123_000_000),
            request_id: "req-1",
            traceparent: "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
            key_id: "k1",
            tenant: None,
            ip: "10.0.0.9".parse().unwrap(),
            method: "GET",
            path: "/api/users",
            query: "q=\"x\"",
            status: 200,
            bytes: 42,
            seconds: 0.00125,
            user_agent: Some("curl/8"),
            referrer: None,
        });
        assert!(l.ends_with("}\n"));
        let v: serde_json::Value = serde_json::from_str(&l).unwrap();
        assert_eq!(v["@timestamp"], "2023-11-14T22:13:20.123Z");
        assert_eq!(v["event"], "http_access");
        assert_eq!(v["trace.id"], "0af7651916cd43dd8448eb211c80319c");
        assert_eq!(v["span.id"], "b7ad6b7169203331");
        assert_eq!(v["http.request.query"], "q=\"x\"");
        assert_eq!(v["http.response.status_code"], 200);
        assert_eq!(v["http.response.bytes"], 42);
        assert_eq!(v["http.duration_ms"], 1.25);
        assert_eq!(v["user_agent.original"], "curl/8");
        assert!(v["http.referrer"].is_null() && v["tenant_id"].is_null());
        assert_eq!(v["client.ip"], "10.0.0.9");
    }
}
