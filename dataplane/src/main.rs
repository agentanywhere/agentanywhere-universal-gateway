//! AgentAnywhere data plane, phase 1 (experimental): see DESIGN.md.
//!
//! dataplane --listen 127.0.0.1:8000 --snapshot snapshot.json \
//!           --backend http://127.0.0.1:9100 --control http://127.0.0.1:8001
mod limits;
mod proxy;
mod snapshot;
mod sync;

use arc_swap::ArcSwap;
use hyper::server::conn::http1;
use hyper::service::service_fn;
use hyper_util::client::legacy::{connect::HttpConnector, Client};
use hyper_util::rt::{TokioExecutor, TokioIo};
use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;
use tokio::net::TcpListener;

struct Args {
    listen: SocketAddr,
    snapshot: Option<String>,
    backend: String,
    control: String,
    token: Option<String>,
    interval: Duration,
    max_staleness: Duration,
}

fn parse_args() -> Result<Args, String> {
    let (mut listen, mut snapshot, mut backend, mut control) =
        ("127.0.0.1:8000".to_string(), String::new(), String::new(), String::new());
    let mut token = std::env::var("DATAPLANE_TOKEN").ok().filter(|t| !t.is_empty());
    let (mut interval_ms, mut staleness_ms) = (500u64, 5000u64);
    let mut it = std::env::args().skip(1);
    while let Some(flag) = it.next() {
        let mut value = || it.next().ok_or_else(|| format!("{flag} needs a value"));
        match flag.as_str() {
            "--listen" => listen = value()?,
            "--snapshot" => snapshot = value()?,
            "--backend" => backend = value()?,
            "--control" => control = value()?,
            "--control-token" => token = Some(value()?),
            "--sync-interval-ms" => interval_ms = value()?.parse().map_err(|e| format!("--sync-interval-ms: {e}"))?,
            "--max-staleness-ms" => staleness_ms = value()?.parse().map_err(|e| format!("--max-staleness-ms: {e}"))?,
            other => return Err(format!("unknown option {other}")),
        }
    }
    if backend.is_empty() || control.is_empty() || (snapshot.is_empty() && token.is_none()) {
        return Err("--backend and --control are required, and either --control-token (or DATAPLANE_TOKEN) or --snapshot".into());
    }
    Ok(Args {
        listen: listen.parse().map_err(|e| format!("--listen: {e}"))?,
        snapshot: (!snapshot.is_empty()).then_some(snapshot),
        backend: backend.trim_end_matches('/').to_string(),
        control: control.trim_end_matches('/').to_string(),
        token,
        interval: Duration::from_millis(interval_ms),
        max_staleness: Duration::from_millis(staleness_ms),
    })
}

#[tokio::main]
async fn main() {
    let args = match parse_args() {
        Ok(a) => a,
        Err(e) => {
            eprintln!("dataplane: {e}");
            std::process::exit(2);
        }
    };
    // A file bootstraps the snapshot; with a token it is replaced by the control plane's, and until
    // the first answer arrives nothing is served natively.
    let snap = match &args.snapshot {
        Some(path) => {
            let text = std::fs::read_to_string(path).unwrap_or_else(|e| {
                eprintln!("dataplane: cannot read {path}: {e}");
                std::process::exit(2);
            });
            snapshot::Snapshot::from_json(&text).unwrap_or_else(|e| {
                eprintln!("dataplane: {e}");
                std::process::exit(2);
            })
        }
        None => snapshot::Snapshot::empty(),
    };
    let link = args.token.as_ref().map(|_| Arc::new(sync::Link::new(args.max_staleness)));

    let mut connector = HttpConnector::new();
    connector.set_nodelay(true);
    let client = Client::builder(TokioExecutor::new())
        .pool_idle_timeout(Duration::from_secs(30))
        .pool_max_idle_per_host(1024)
        .build(connector);
    let state = Arc::new(proxy::State {
        snapshot: ArcSwap::from_pointee(snap),
        backend: args.backend,
        control: args.control.clone(),
        client,
        sync: link,
    });
    if let Some(token) = args.token.clone() {
        tokio::spawn(sync::run(state.clone(), args.control.clone(), token, args.interval));
    }

    let listener = TcpListener::bind(args.listen).await.unwrap_or_else(|e| {
        eprintln!("dataplane: cannot listen on {}: {e}", args.listen);
        std::process::exit(2);
    });
    eprintln!("dataplane: listening on {}", args.listen);
    loop {
        let Ok((stream, peer)) = listener.accept().await else { continue };
        let _ = stream.set_nodelay(true);
        let state = state.clone();
        tokio::spawn(async move {
            let svc = service_fn(move |req| proxy::handle(state.clone(), peer, req));
            let _ = http1::Builder::new().serve_connection(TokioIo::new(stream), svc).await;
        });
    }
}
