//! AgentAnywhere data plane, phase 1 (experimental): see DESIGN.md.
//!
//! dataplane --listen 127.0.0.1:8000 --snapshot snapshot.json \
//!           --backend http://127.0.0.1:9100 --control http://127.0.0.1:8001
mod limits;
mod proxy;
mod snapshot;

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
    snapshot: String,
    backend: String,
    control: String,
}

fn parse_args() -> Result<Args, String> {
    let (mut listen, mut snapshot, mut backend, mut control) =
        ("127.0.0.1:8000".to_string(), String::new(), String::new(), String::new());
    let mut it = std::env::args().skip(1);
    while let Some(flag) = it.next() {
        let mut value = || it.next().ok_or_else(|| format!("{flag} needs a value"));
        match flag.as_str() {
            "--listen" => listen = value()?,
            "--snapshot" => snapshot = value()?,
            "--backend" => backend = value()?,
            "--control" => control = value()?,
            other => return Err(format!("unknown option {other}")),
        }
    }
    if snapshot.is_empty() || backend.is_empty() || control.is_empty() {
        return Err("--snapshot, --backend and --control are required".into());
    }
    Ok(Args {
        listen: listen.parse().map_err(|e| format!("--listen: {e}"))?,
        snapshot,
        backend: backend.trim_end_matches('/').to_string(),
        control: control.trim_end_matches('/').to_string(),
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
    let text = std::fs::read_to_string(&args.snapshot).unwrap_or_else(|e| {
        eprintln!("dataplane: cannot read {}: {e}", args.snapshot);
        std::process::exit(2);
    });
    let snap = snapshot::Snapshot::from_json(&text).unwrap_or_else(|e| {
        eprintln!("dataplane: {e}");
        std::process::exit(2);
    });

    let mut connector = HttpConnector::new();
    connector.set_nodelay(true);
    let client = Client::builder(TokioExecutor::new())
        .pool_idle_timeout(Duration::from_secs(30))
        .pool_max_idle_per_host(1024)
        .build(connector);
    let state = Arc::new(proxy::State {
        snapshot: ArcSwap::from_pointee(snap),
        backend: args.backend,
        control: args.control,
        client,
    });

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
