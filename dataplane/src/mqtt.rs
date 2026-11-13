//! The data plane's own connection to the MQTT broker, for `POST /mqtt/publish`.
//!
//! The Python gateway holds one MQTT 5 connection to the broker and publishes on it what clients
//! post. So does this side: one connection for the whole process, on a thread of its own, to the
//! broker the snapshot names. A worker hands a message over only while that connection stands;
//! at any other time the request goes to the Python gateway, which publishes on its own.
//!
//! As with the Python gateway's client, a request is answered once the message has been handed
//! to the connection, not once the broker has acknowledged it. Messages above quality of service
//! 0 are kept until the broker has, and sent again after a reconnect.
use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio::sync::mpsc::{unbounded_channel, UnboundedReceiver, UnboundedSender};
use tokio::time::timeout;

use crate::proxy::State;
use crate::pyjson::Publish;
use crate::snapshot::Broker;

/// The keep-alive the Python gateway's client asks for, in seconds.
const KEEP_ALIVE: u16 = 60;
/// How long without sending anything before a ping, so the broker never waits the full keep-alive.
const PING_AFTER: Duration = Duration::from_secs(30);
/// How long a ping may go unanswered before the connection is given up.
const PING_TIMEOUT: Duration = Duration::from_secs(20);
const CONNECT_TIMEOUT: Duration = Duration::from_secs(10);
/// How often the connection looks up from waiting: for a ping that is due, or a broker that changed.
const TICK: Duration = Duration::from_millis(250);
/// The wait before connecting again, doubled after each failure up to the longest.
const RETRY: (Duration, Duration) = (Duration::from_secs(1), Duration::from_secs(30));
/// Messages above quality of service 0 awaiting the broker at once: the Python client's limit.
const MAX_IN_FLIGHT: usize = 20;
/// Nothing a broker sends a client that subscribes to nothing comes near this.
const MAX_PACKET: usize = 1 << 20;

pub enum Event {
    Publish(Publish),
    /// A packet from the broker on the numbered connection: its first byte and the rest.
    Packet(u64, u8, Vec<u8>),
    /// The numbered connection ended.
    Closed(u64),
}

/// What the workers see of the connection.
pub struct Link {
    up: AtomicBool,
    /// The broker the connection stands to, while it does.
    to: Mutex<Option<Broker>>,
    events: UnboundedSender<Event>,
}

impl Link {
    pub fn new() -> (Arc<Link>, UnboundedReceiver<Event>) {
        let (events, received) = unbounded_channel();
        (Arc::new(Link { up: AtomicBool::new(false), to: Mutex::new(None), events }), received)
    }

    /// Whether a message for `broker` can be handed over now.
    pub fn serves(&self, broker: &Broker) -> bool {
        self.up.load(Ordering::Acquire) && self.to.lock().unwrap().as_ref() == Some(broker)
    }

    pub fn publish(&self, message: Publish) {
        let _ = self.events.send(Event::Publish(message));
    }

    fn set(&self, to: Option<&Broker>) {
        *self.to.lock().unwrap() = to.cloned();
        self.up.store(to.is_some(), Ordering::Release);
    }
}

/// A message the broker has yet to acknowledge.
struct Flying {
    id: u16,
    message: Publish,
    /// At quality of service 2: the broker has the message, and is owed the release.
    released: bool,
}

/// What outlives a connection: the messages owed to the broker.
#[derive(Default)]
struct Session {
    last_id: u16,
    flying: Vec<Flying>,
    waiting: VecDeque<Publish>,
}

impl Session {
    fn next_id(&mut self) -> u16 {
        loop {
            self.last_id = if self.last_id == u16::MAX { 1 } else { self.last_id + 1 };
            if !self.flying.iter().any(|f| f.id == self.last_id) {
                return self.last_id;
            }
        }
    }

    fn landed(&mut self, id: u16) {
        self.flying.retain(|f| f.id != id);
    }

    /// The packets for what can be sent now, in order.
    fn due(&mut self) -> Vec<u8> {
        let mut out = Vec::new();
        while let Some(next) = self.waiting.front() {
            if next.qos > 0 && self.flying.len() >= MAX_IN_FLIGHT {
                break;
            }
            let Some(message) = self.waiting.pop_front() else { break };
            if message.qos == 0 {
                out.extend(publish_packet(&message, 0, false));
            } else {
                let id = self.next_id();
                out.extend(publish_packet(&message, id, false));
                self.flying.push(Flying { id, message, released: false });
            }
        }
        out
    }

    /// The packets that pick up, on a new connection, where the last one left off.
    fn again(&self) -> Vec<u8> {
        let mut out = Vec::new();
        for f in &self.flying {
            match f.released {
                true => out.extend(release_packet(f.id)),
                false => out.extend(publish_packet(&f.message, f.id, true)),
            }
        }
        out
    }
}

fn length(mut n: usize, out: &mut Vec<u8>) {
    loop {
        let byte = (n % 128) as u8;
        n /= 128;
        out.push(if n > 0 { byte | 0x80 } else { byte });
        if n == 0 {
            return;
        }
    }
}

/// CONNECT for MQTT 5 with nothing but a client identifier, as the Python gateway's client sends it.
fn connect_packet(client_id: &str, clean_start: bool) -> Vec<u8> {
    let mut body = vec![0, 4, b'M', b'Q', b'T', b'T', 5, if clean_start { 2 } else { 0 }];
    body.extend_from_slice(&KEEP_ALIVE.to_be_bytes());
    body.push(0);
    body.extend_from_slice(&(client_id.len() as u16).to_be_bytes());
    body.extend_from_slice(client_id.as_bytes());
    let mut out = vec![0x10];
    length(body.len(), &mut out);
    out.extend(body);
    out
}

/// PUBLISH without properties and never retained, as the Python gateway's client sends it.
fn publish_packet(message: &Publish, id: u16, again: bool) -> Vec<u8> {
    let identified = message.qos > 0;
    let mut out = vec![0x30 | (u8::from(again) << 3) | (message.qos << 1)];
    length(2 + message.topic.len() + if identified { 2 } else { 0 } + 1 + message.payload.len(), &mut out);
    out.extend_from_slice(&(message.topic.len() as u16).to_be_bytes());
    out.extend_from_slice(message.topic.as_bytes());
    if identified {
        out.extend_from_slice(&id.to_be_bytes());
    }
    out.push(0);
    out.extend_from_slice(&message.payload);
    out
}

fn release_packet(id: u16) -> [u8; 4] {
    let id = id.to_be_bytes();
    [0x62, 2, id[0], id[1]]
}

async fn read_packet(from: &mut (impl AsyncRead + Unpin)) -> std::io::Result<(u8, Vec<u8>)> {
    let first = from.read_u8().await?;
    let (mut len, mut shift) = (0usize, 0);
    loop {
        let byte = from.read_u8().await?;
        len |= usize::from(byte & 0x7f) << shift;
        if byte & 0x80 == 0 {
            break;
        }
        shift += 7;
        if shift > 21 {
            return Err(std::io::Error::other("a packet length that is not one"));
        }
    }
    if len > MAX_PACKET {
        return Err(std::io::Error::other("a packet too large to be meant for this client"));
    }
    let mut body = vec![0; len];
    from.read_exact(&mut body).await?;
    Ok((first, body))
}

/// The connection, with or without TLS under it.
trait Wire: AsyncRead + AsyncWrite + Unpin + Send {}
impl<T: AsyncRead + AsyncWrite + Unpin + Send> Wire for T {}

/// Ends the task that reads from the broker when the connection it belongs to is done with.
struct Reading(tokio::task::JoinHandle<()>);

impl Drop for Reading {
    fn drop(&mut self) {
        self.0.abort();
    }
}

/// One connection, from CONNECT until it fails (`Err`) or the snapshot names another broker (`Ok`).
async fn serve(
    state: &State,
    link: &Link,
    broker: &Broker,
    number: u64,
    client_id: &str,
    events: &mut UnboundedReceiver<Event>,
    session: &mut Session,
) -> Result<(), String> {
    let said = |e: std::io::Error| e.to_string();
    let stream = timeout(CONNECT_TIMEOUT, TcpStream::connect((broker.host.as_str(), broker.port)))
        .await
        .map_err(|_| "no connection in time".to_owned())?
        .map_err(said)?;
    let _ = stream.set_nodelay(true);
    let wire: Box<dyn Wire> = if broker.tls {
        // Verified as the Python gateway's client verifies it: the certificate, and that it is the host's.
        let config = crate::proxy::tls_for_brokers().ok_or("is behind TLS, and this side has no certificates to verify it against")?;
        let name = rustls::pki_types::ServerName::try_from(broker.host.clone()).map_err(|e| e.to_string())?;
        let secured = timeout(CONNECT_TIMEOUT, tokio_rustls::TlsConnector::from(config).connect(name, stream))
            .await
            .map_err(|_| "no TLS session in time".to_owned())?
            .map_err(said)?;
        Box::new(secured)
    } else {
        Box::new(stream)
    };
    let (mut read, mut write) = tokio::io::split(wire);
    write.write_all(&connect_packet(client_id, number == 1)).await.map_err(said)?;
    let (first, body) = timeout(CONNECT_TIMEOUT, read_packet(&mut read)).await.map_err(|_| "no answer to CONNECT in time".to_owned())?.map_err(said)?;
    match (first >> 4, body.get(1)) {
        (2, Some(0)) => {}
        (2, Some(reason)) => return Err(format!("refused the connection (reason {reason:#04x})")),
        _ => return Err("answered CONNECT with something else".into()),
    }
    let to_here = link.events.clone();
    let _reading = Reading(tokio::spawn(async move {
        loop {
            let event = match read_packet(&mut read).await {
                Ok((first, body)) => Event::Packet(number, first, body),
                Err(_) => Event::Closed(number),
            };
            let ended = matches!(event, Event::Closed(_));
            if to_here.send(event).is_err() || ended {
                return;
            }
        }
    }));
    write.write_all(&session.again()).await.map_err(said)?;
    link.set(Some(broker));
    let mut last_sent = Instant::now();
    let mut pinged: Option<Instant> = None;
    loop {
        match timeout(TICK, events.recv()).await {
            Ok(Some(Event::Publish(message))) => session.waiting.push_back(message),
            Ok(Some(Event::Packet(n, first, body))) if n == number => {
                let id = body.get(..2).map(|b| u16::from_be_bytes([b[0], b[1]]));
                match (first >> 4, id) {
                    // PUBACK and PUBCOMP: the message is the broker's now, or it never will be.
                    (4 | 7, Some(id)) => session.landed(id),
                    // PUBREC: received, unless it says otherwise; then released.
                    (5, Some(id)) if body.get(2).is_some_and(|reason| *reason >= 0x80) => session.landed(id),
                    (5, Some(id)) => {
                        if let Some(f) = session.flying.iter_mut().find(|f| f.id == id) {
                            f.released = true;
                        }
                        write.write_all(&release_packet(id)).await.map_err(said)?;
                        last_sent = Instant::now();
                    }
                    (13, _) => pinged = None,
                    (14, _) => return Err("ended the session".into()),
                    _ => {}
                }
            }
            Ok(Some(Event::Closed(n))) if n == number => return Err("closed the connection".into()),
            // From a connection that is gone.
            Ok(Some(_)) => {}
            Ok(None) => return Ok(()),
            Err(_) => {}
        }
        let due = session.due();
        if !due.is_empty() {
            write.write_all(&due).await.map_err(said)?;
            last_sent = Instant::now();
        }
        if state.snapshot.load().mqtt.as_ref() != Some(broker) {
            let _ = write.write_all(&[0xe0, 0]).await;
            return Ok(());
        }
        match pinged {
            Some(at) if at.elapsed() > PING_TIMEOUT => return Err("stopped answering".into()),
            None if last_sent.elapsed() >= PING_AFTER => {
                write.write_all(&[0xc0, 0]).await.map_err(said)?;
                (pinged, last_sent) = (Some(Instant::now()), Instant::now());
            }
            _ => {}
        }
    }
}

async fn run(state: Arc<State>, mut events: UnboundedReceiver<Event>) {
    let link = state.mqtt.clone();
    let client_id = format!("gateway_dataplane_{:08x}", fastrand::u32(..));
    let mut session = Session::default();
    let (mut number, mut wait, mut failing) = (0u64, RETRY.0, false);
    loop {
        let Some(broker) = state.snapshot.load().mqtt.clone() else {
            // No broker to speak to: nothing is owed to anyone.
            session = Session::default();
            while events.try_recv().is_ok() {}
            tokio::time::sleep(TICK).await;
            continue;
        };
        number += 1;
        let started = Instant::now();
        let ended = serve(&state, &link, &broker, number, &client_id, &mut events, &mut session).await;
        let was_up = link.up.load(Ordering::Acquire);
        link.set(None);
        let Err(why) = ended else { continue };
        // Said once per outage, not once per attempt.
        if was_up || !failing {
            eprintln!("dataplane: MQTT broker {}:{} {why}; publishing is left to the control plane until it is back", broker.host, broker.port);
        }
        failing = true;
        if was_up && started.elapsed() > RETRY.1 {
            (wait, failing) = (RETRY.0, false);
        }
        tokio::time::sleep(wait).await;
        wait = (wait * 2).min(RETRY.1);
    }
}

/// Start the thread that keeps the connection to the broker.
pub fn start(state: Arc<State>, events: UnboundedReceiver<Event>) -> std::io::Result<()> {
    std::thread::Builder::new().name("mqtt".into()).spawn(move || {
        let rt = tokio::runtime::Builder::new_current_thread().enable_all().build().expect("mqtt runtime");
        rt.block_on(run(state, events));
    })?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn message(qos: u8) -> Publish {
        Publish { topic: "a/b".into(), qos, payload: b"{}".to_vec() }
    }

    #[test]
    fn packets_are_the_ones_the_python_gateways_client_sends() {
        // What that client was seen to send, but for the identifier.
        assert_eq!(connect_packet("gw", true), [0x10, 15, 0, 4, b'M', b'Q', b'T', b'T', 5, 2, 0, 60, 0, 0, 2, b'g', b'w']);
        assert_eq!(connect_packet("gw", false)[9], 0);
        assert_eq!(publish_packet(&message(0), 0, false), [0x30, 8, 0, 3, b'a', b'/', b'b', 0, b'{', b'}']);
        assert_eq!(publish_packet(&message(1), 7, false), [0x32, 10, 0, 3, b'a', b'/', b'b', 0, 7, 0, b'{', b'}']);
        assert_eq!(publish_packet(&message(2), 258, true), [0x3c, 10, 0, 3, b'a', b'/', b'b', 1, 2, 0, b'{', b'}']);
        assert_eq!(release_packet(258), [0x62, 2, 1, 2]);
        let long = Publish { topic: "t".into(), qos: 0, payload: vec![b'x'; 200] };
        assert_eq!(publish_packet(&long, 0, false)[..3], [0x30, 0xcc, 0x01]);
    }

    #[test]
    fn only_so_many_messages_await_the_broker_at_once() {
        let mut s = Session::default();
        for _ in 0..MAX_IN_FLIGHT + 2 {
            s.waiting.push_back(message(1));
        }
        s.waiting.push_back(message(0));
        assert!(!s.due().is_empty());
        assert_eq!((s.flying.len(), s.waiting.len()), (MAX_IN_FLIGHT, 3));
        s.landed(1);
        s.landed(2);
        s.due();
        // The message that needs no acknowledgement kept its place in the queue.
        assert_eq!((s.flying.len(), s.waiting.len()), (MAX_IN_FLIGHT, 0));
    }

    #[test]
    fn what_the_broker_has_not_acknowledged_is_sent_again() {
        let mut s = Session::default();
        s.waiting.extend([message(1), message(2)]);
        s.due();
        s.flying[1].released = true;
        let mut again = publish_packet(&message(1), 1, true);
        again.extend(release_packet(2));
        assert_eq!(s.again(), again);
        s.last_id = u16::MAX;
        assert_eq!(s.next_id(), 3);
    }

    #[test]
    fn a_message_is_handed_over_only_while_the_connection_to_its_broker_stands() {
        let (link, _events) = Link::new();
        let here = Broker { host: "broker".into(), port: 1883, tls: false };
        assert!(!link.serves(&here));
        link.set(Some(&here));
        assert!(link.serves(&here) && !link.serves(&Broker { host: "broker".into(), port: 1884, tls: false }) && !link.serves(&Broker { tls: true, ..here.clone() }));
        link.set(None);
        assert!(!link.serves(&here));
    }
}
