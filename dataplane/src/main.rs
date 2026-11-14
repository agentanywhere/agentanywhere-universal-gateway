//! AgentAnywhere data plane (experimental): see DESIGN.md.
//!
//! dataplane --listen 127.0.0.1:8000 --control http://127.0.0.1:8001 \
//!           --backend http://127.0.0.1:9100 --control-token <DATAPLANE_TOKEN>
//!
//! One thread per core, each with its own runtime, connections and counters; one more for the
//! conversation with the control plane.
mod accesslog;
mod boundary;
mod breaker;
mod govern;
mod limits;
mod mqtt;
mod proxy;
mod pyjson;
mod report;
mod schema;
mod snapshot;
mod sync;
mod tenant;

use arc_swap::ArcSwap;
use hyper::server::conn::http1;
use hyper::service::service_fn;
use hyper_util::rt::{TokioExecutor, TokioIo};
use hyper_util::server::conn::auto;
use socket2::{Domain, Protocol, Socket, Type};
use std::net::SocketAddr;
use std::sync::atomic::AtomicU64;
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
    report_interval: Duration,
    rows_per_second: u32,
    workers: usize,
    access_log: bool,
}

fn parse_args() -> Result<Args, String> {
    let (mut listen, mut snapshot, mut backend, mut control) =
        ("127.0.0.1:8000".to_string(), String::new(), String::new(), String::new());
    let mut token = std::env::var("DATAPLANE_TOKEN").ok().filter(|t| !t.is_empty());
    let (mut interval_ms, mut staleness_ms) = (500u64, 5000u64);
    let (mut report_ms, mut rows_per_sec) = (1000u64, 1000u32);
    let mut workers = std::thread::available_parallelism().map(|n| n.get()).unwrap_or(1);
    let mut access_log = false;
    let mut it = std::env::args().skip(1);
    while let Some(flag) = it.next() {
        if flag == "--access-log" {
            access_log = true;
            continue;
        }
        let mut value = || it.next().ok_or_else(|| format!("{flag} needs a value"));
        match flag.as_str() {
            "--workers" => workers = value()?.parse().map_err(|e| format!("--workers: {e}"))?,
            "--listen" => listen = value()?,
            "--snapshot" => snapshot = value()?,
            "--backend" => backend = value()?,
            "--control" => control = value()?,
            "--control-token" => token = Some(value()?),
            "--sync-interval-ms" => interval_ms = value()?.parse().map_err(|e| format!("--sync-interval-ms: {e}"))?,
            "--report-interval-ms" => report_ms = value()?.parse().map_err(|e| format!("--report-interval-ms: {e}"))?,
            "--log-rows-per-second" => rows_per_sec = value()?.parse().map_err(|e| format!("--log-rows-per-second: {e}"))?,
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
        report_interval: Duration::from_millis(report_ms),
        rows_per_second: rows_per_sec,
        workers: workers.max(1),
        access_log,
    })
}

/// A listening socket. With `shared`, other sockets may listen on the same address and the
/// kernel spreads new connections across them.
fn listen(addr: SocketAddr, shared: bool) -> std::io::Result<std::net::TcpListener> {
    let socket = Socket::new(Domain::for_address(addr), Type::STREAM, Some(Protocol::TCP))?;
    socket.set_reuse_address(true)?;
    #[cfg(unix)]
    if shared {
        socket.set_reuse_port(true)?;
    }
    #[cfg(not(unix))]
    let _ = shared;
    socket.set_nonblocking(true)?;
    socket.bind(&addr.into())?;
    socket.listen(4096)?;
    Ok(socket.into())
}

/// Where a worker's connections come from.
enum Intake {
    /// A listening socket of its own on the shared address: the kernel hands each new connection
    /// to one worker, and it stays on that core.
    Own(std::net::TcpListener),
    /// Connections accepted on one socket by the acceptor thread and dealt out in turn.
    Dealt(tokio::sync::mpsc::UnboundedReceiver<(std::net::TcpStream, SocketAddr)>),
}

/// One intake per worker. On Linux each worker listens for itself; elsewhere sockets sharing an
/// address do not share its connections evenly, so one thread accepts and deals them out.
fn intakes(addr: SocketAddr, workers: usize) -> std::io::Result<Vec<Intake>> {
    if cfg!(target_os = "linux") {
        let first = listen(addr, true)?;
        // With port 0 the system picks the port: the others must ask for that one.
        let addr = first.local_addr()?;
        let mut all = vec![Intake::Own(first)];
        for _ in 1..workers {
            all.push(Intake::Own(listen(addr, true)?));
        }
        return Ok(all);
    }
    let listener = listen(addr, false)?;
    listener.set_nonblocking(false)?;
    let (senders, receivers): (Vec<_>, Vec<_>) = (0..workers).map(|_| tokio::sync::mpsc::unbounded_channel()).unzip();
    std::thread::Builder::new().name("accept".into()).spawn(move || {
        let mut next = 0;
        loop {
            match listener.accept() {
                Ok(connection) => {
                    let _ = senders[next].send(connection);
                    next = (next + 1) % senders.len();
                }
                // Out of file descriptors, most likely: give connections time to close.
                Err(_) => std::thread::sleep(Duration::from_millis(10)),
            }
        }
    })?;
    Ok(receivers.into_iter().map(Intake::Dealt).collect())
}

/// What a client that speaks HTTP/2 from its first byte sends first.
const HTTP2_PREFACE: &[u8] = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n";

fn connected(worker: &Arc<proxy::Worker>, stream: tokio::net::TcpStream, peer: SocketAddr) {
    let _ = stream.set_nodelay(true);
    let worker = worker.clone();
    tokio::spawn(async move {
        // HTTP/2 without TLS starts with a fixed preface (gRPC clients connect this way). Only a
        // connection that begins like one pays for telling the two apart.
        let mut first = [0u8; 24];
        let http2 = matches!(stream.peek(&mut first).await, Ok(n) if n > 0 && HTTP2_PREFACE.starts_with(&first[..n]));
        let svc = service_fn(move |req| proxy::handle(worker.clone(), peer, req));
        if http2 {
            let _ = auto::Builder::new(TokioExecutor::new()).serve_connection_with_upgrades(TokioIo::new(stream), svc).await;
        } else {
            // With upgrades: a WebSocket handshake that is accepted turns the connection into a tunnel.
            let _ = http1::Builder::new().serve_connection(TokioIo::new(stream), svc).with_upgrades().await;
        }
    });
}

/// Serve connections on one core until the process ends.
fn serve(index: usize, intake: Intake, state: Arc<proxy::State>) {
    let rt = tokio::runtime::Builder::new_current_thread().enable_all().build().expect("worker runtime");
    rt.block_on(async move {
        let worker = Arc::new(proxy::Worker::new(state, index));
        match intake {
            Intake::Own(listener) => {
                let listener = TcpListener::from_std(listener).expect("listener");
                loop {
                    match listener.accept().await {
                        Ok((stream, peer)) => connected(&worker, stream, peer),
                        Err(_) => tokio::time::sleep(Duration::from_millis(10)).await,
                    }
                }
            }
            Intake::Dealt(mut dealt) => {
                while let Some((stream, peer)) = dealt.recv().await {
                    if stream.set_nonblocking(true).is_err() {
                        continue;
                    }
                    if let Ok(stream) = tokio::net::TcpStream::from_std(stream) {
                        connected(&worker, stream, peer);
                    }
                }
            }
        }
    });
}

fn fail(message: String) -> ! {
    eprintln!("dataplane: {message}");
    std::process::exit(2);
}

fn main() {
    // `dataplane --judge-bodies` answers, for cases on stdin, what the boundary checks make of
    // each body. The conformance suite compares the answers with the Python gateway's.
    if std::env::args().nth(1).as_deref() == Some("--judge-bodies") {
        return boundary::judge_stdin().unwrap_or_else(|e| fail(e));
    }
    // `dataplane --judge-publishes` answers, for bodies on stdin, what `POST /mqtt/publish` would
    // publish for each.
    // `dataplane --judge-patterns` answers which paths each transformation rule's pattern matches.
    if std::env::args().nth(1).as_deref() == Some("--judge-patterns") {
        return snapshot::judge_patterns().unwrap_or_else(|e| fail(e));
    }
    // `dataplane --judge-shapes` answers what transformation rules make of a request and its answer.
    if std::env::args().nth(1).as_deref() == Some("--judge-shapes") {
        return proxy::judge_shapes().unwrap_or_else(|e| fail(e));
    }
    // `dataplane --judge-preflights` answers, for preflights on stdin, what a web page is told.
    if std::env::args().nth(1).as_deref() == Some("--judge-preflights") {
        return proxy::judge_preflights().unwrap_or_else(|e| fail(e));
    }
    // `dataplane --judge-paths` answers, for paths on stdin, how each is read and what a backend is asked for.
    if std::env::args().nth(1).as_deref() == Some("--judge-paths") {
        return proxy::judge_paths().unwrap_or_else(|e| fail(e));
    }
    if std::env::args().nth(1).as_deref() == Some("--judge-publishes") {
        return pyjson::judge_stdin().unwrap_or_else(|e| fail(e));
    }
    let args = parse_args().unwrap_or_else(|e| fail(e));
    // A file bootstraps the snapshot; with a token it is replaced by the control plane's, and until
    // the first answer arrives nothing is served natively.
    let snap = match &args.snapshot {
        Some(path) => {
            let text = std::fs::read_to_string(path).unwrap_or_else(|e| fail(format!("cannot read {path}: {e}")));
            snapshot::Snapshot::from_json(&text).unwrap_or_else(|e| fail(e))
        }
        None => snapshot::Snapshot::empty(),
    };
    let link = args.token.as_ref().map(|_| Arc::new(sync::Link::new(args.max_staleness)));
    let reporter = args.token.as_ref().map(|_| Arc::new(report::Reporter::new(args.rows_per_second, 5000, args.workers)));
    if let Err(why) = proxy::start_tls() {
        eprintln!("dataplane: backends behind TLS are left to the control plane ({why})");
    }
    let backend = proxy::Backend::parse(&args.backend).unwrap_or_else(|e| fail(format!("--backend {e}")));
    let (mqtt, mqtt_events) = mqtt::Link::new();
    let state = Arc::new(proxy::State {
        snapshot: ArcSwap::from_pointee(snapshot::Snapshot::empty()),
        generation: AtomicU64::new(0),
        denied: Default::default(),
        backends: proxy::Backends::with(&backend),
        backend,
        control: proxy::Upstream::parse(&args.control).unwrap_or_else(|e| fail(format!("--control {e}"))),
        sync: link,
        reporter,
        access_log: args.access_log.then(accesslog::AccessLog::start),
        mqtt,
    });
    state.replace(snap);
    mqtt::start(state.clone(), mqtt_events).unwrap_or_else(|e| fail(format!("cannot start the MQTT thread: {e}")));
    let sockets = intakes(args.listen, args.workers).unwrap_or_else(|e| fail(format!("cannot listen on {}: {e}", args.listen)));

    // The conversation with the control plane has a thread of its own, so no amount of traffic
    // delays hearing about a revoked key.
    if let Some(token) = args.token.clone() {
        let state = state.clone();
        let (interval, report_interval) = (args.interval, args.report_interval);
        std::thread::Builder::new()
            .name("control".into())
            .spawn(move || {
                let rt = tokio::runtime::Builder::new_current_thread().enable_all().build().expect("control runtime");
                rt.block_on(sync::run(state, token, interval, report_interval));
            })
            .unwrap_or_else(|e| fail(format!("cannot start the control thread: {e}")));
    }

    eprintln!("dataplane: listening on {} with {} workers", args.listen, args.workers);
    let threads: Vec<_> = sockets
        .into_iter()
        .enumerate()
        .map(|(index, socket)| {
            let state = state.clone();
            std::thread::Builder::new()
                .name(format!("worker-{index}"))
                .spawn(move || serve(index, socket, state))
                .unwrap_or_else(|e| fail(format!("cannot start a worker: {e}")))
        })
        .collect();
    for t in threads {
        let _ = t.join();
    }
}
