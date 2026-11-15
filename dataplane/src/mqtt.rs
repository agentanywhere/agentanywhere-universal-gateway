//! The data plane's own connection to the MQTT broker, for `POST /mqtt/publish` and `POST /mqtt/subscribe`.
//!
//! The Python gateway holds one MQTT 5 connection to the broker and publishes on it what clients
//! post. So does this side: one connection for the whole process, on a thread of its own, to the
//! broker the snapshot names. A worker hands a message over only while that connection stands;
//! at any other time the request goes to the Python gateway, which publishes on its own.
//!
//! As with the Python gateway's client, a request is answered once the message has been handed
//! to the connection, not once the broker has acknowledged it. Messages above quality of service
//! 0 are kept until the broker has, and sent again after a reconnect.
//!
//! A subscription is this connection's, as one made through the Python gateway is that gateway's
//! connection's. What the broker then sends is acknowledged and written to this process's output,
//! one line a message, as the Python gateway writes it to its log; nothing else is done with it
//! on either side. A subscription ends with the connection, here as there: neither client asks
//! the broker to keep its session, and neither subscribes again after a reconnect.
use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio::sync::mpsc::{unbounded_channel, UnboundedReceiver, UnboundedSender};
use tokio::time::timeout;

use crate::proxy::State;
use crate::pyjson::{Publish, Subscribe};
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
/// The largest packet taken from the broker. A message above it ends the connection, and with
/// it the subscription that brought it.
const MAX_PACKET: usize = 16 << 20;

pub enum Event {
    Publish(Publish),
    Subscribe(Subscribe),
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
    /// Where the messages the broker sends are written.
    lines: crate::accesslog::Lines,
}

impl Link {
    pub fn new() -> (Arc<Link>, UnboundedReceiver<Event>) {
        let (events, received) = unbounded_channel();
        (Arc::new(Link { up: AtomicBool::new(false), to: Mutex::new(None), events, lines: crate::accesslog::Lines::default() }), received)
    }

    /// Whether a message for `broker` can be handed over now.
    pub fn serves(&self, broker: &Broker) -> bool {
        self.up.load(Ordering::Acquire) && self.to.lock().unwrap().as_ref() == Some(broker)
    }

    pub fn publish(&self, message: Publish) {
        let _ = self.events.send(Event::Publish(message));
    }

    pub fn subscribe(&self, to: Subscribe) {
        let _ = self.events.send(Event::Subscribe(to));
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
    /// Subscriptions not yet asked of the broker.
    subscribing: VecDeque<Subscribe>,
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
        while let Some(wanted) = self.subscribing.pop_front() {
            let id = self.next_id();
            out.extend(subscribe_packet(&wanted, id));
        }
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

/// SUBSCRIBE to one filter, without properties and with no option but the quality of service, as
/// the Python gateway's client sends it.
fn subscribe_packet(wanted: &Subscribe, id: u16) -> Vec<u8> {
    let mut out = vec![0x82];
    length(2 + 1 + 2 + wanted.topic.len() + 1, &mut out);
    out.extend_from_slice(&id.to_be_bytes());
    out.push(0);
    out.extend_from_slice(&(wanted.topic.len() as u16).to_be_bytes());
    out.extend_from_slice(wanted.topic.as_bytes());
    out.push(wanted.qos);
    out
}

/// A message from the broker: its quality of service, its packet identifier above quality of
/// service 0, its topic and its payload. `None` for a packet that is not a whole PUBLISH.
fn received(first: u8, body: &[u8]) -> Option<(u8, Option<u16>, &[u8], &[u8])> {
    let qos = (first >> 1) & 3;
    let topic_len = usize::from(u16::from_be_bytes([*body.first()?, *body.get(1)?]));
    let topic = body.get(2..2 + topic_len)?;
    let mut at = 2 + topic_len;
    let id = match qos {
        0 => None,
        1 | 2 => {
            let id = u16::from_be_bytes([*body.get(at)?, *body.get(at + 1)?]);
            at += 2;
            Some(id)
        }
        _ => return None,
    };
    // The properties: their length, then as many bytes.
    let (mut properties, mut shift) = (0usize, 0);
    loop {
        let byte = *body.get(at)?;
        at += 1;
        properties |= usize::from(byte & 0x7f) << shift;
        if byte & 0x80 == 0 {
            break;
        }
        shift += 7;
        if shift > 21 {
            return None;
        }
    }
    Some((qos, id, topic, body.get(at + properties..)?))
}

/// The line written for a message from the broker, in the words of the Python gateway's log: the
/// topic and the payload, which is the JSON it spells or else its text under `raw`. A message
/// that is not text is said to be in error, as there.
fn received_line(topic: &[u8], payload: &[u8], at: Duration) -> String {
    let stamp = crate::proxy::timestamp(at, true);
    let rest = format!("\"level\":\"info\",\"logger\":\"dataplane\",\"@timestamp\":\"{stamp}\"}}\n");
    let (Ok(topic), Ok(text)) = (std::str::from_utf8(topic), std::str::from_utf8(payload)) else {
        return format!("{{\"error\":\"a message that is not UTF-8\",\"event\":\"mqtt_message_error\",{rest}");
    };
    let payload = match serde_json::from_str::<serde::de::IgnoredAny>(text) {
        Ok(_) => text.trim().to_owned(),
        Err(_) => format!("{{\"raw\":{}}}", crate::pyjson::quoted(text)),
    };
    format!("{{\"topic\":{},\"payload\":{payload},\"event\":\"mqtt_message_received\",{rest}", crate::pyjson::quoted(topic))
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
    // Messages at quality of service 2 the broker has yet to release: each is written once.
    let mut held: Vec<u16> = Vec::new();
    loop {
        match timeout(TICK, events.recv()).await {
            Ok(Some(Event::Publish(message))) => session.waiting.push_back(message),
            Ok(Some(Event::Subscribe(wanted))) => session.subscribing.push_back(wanted),
            // A message for a subscription made here: written out, and acknowledged.
            Ok(Some(Event::Packet(n, first, body))) if n == number && first >> 4 == 3 => {
                let Some((qos, id, topic, payload)) = received(first, &body) else { return Err("sent a message that is not one".into()) };
                let again = id.is_some_and(|id| qos == 2 && held.contains(&id));
                if !again {
                    let at = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap_or_default();
                    link.lines.write(received_line(topic, payload, at));
                }
                if let Some(id) = id {
                    let [high, low] = id.to_be_bytes();
                    write.write_all(&[if qos == 1 { 0x40 } else { 0x50 }, 2, high, low]).await.map_err(said)?;
                    last_sent = Instant::now();
                    if qos == 2 && !again {
                        held.push(id);
                    }
                }
            }
            // PUBREL: the broker is done with a message at quality of service 2.
            Ok(Some(Event::Packet(n, first, body))) if n == number && first >> 4 == 6 => {
                if let Some(id) = body.get(..2).map(|b| u16::from_be_bytes([b[0], b[1]])) {
                    held.retain(|h| *h != id);
                    let [high, low] = id.to_be_bytes();
                    write.write_all(&[0x70, 2, high, low]).await.map_err(said)?;
                    last_sent = Instant::now();
                }
            }
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
        // A subscription is the connection's: one not yet asked for ends with it too.
        session.subscribing.clear();
        let Err(why) = ended else { continue };
        // Said once per outage, not once per attempt.
        if was_up || !failing {
            eprintln!("dataplane: MQTT broker {}:{} {why}; publishing and subscribing are left to the control plane until it is back", broker.host, broker.port);
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
    fn a_subscription_is_asked_for_as_the_python_gateways_client_asks() {
        // What that client was seen to send, but for the packet identifier.
        let wanted = Subscribe { topic: "s/#".into(), qos: 1 };
        assert_eq!(subscribe_packet(&wanted, 258), [0x82, 9, 1, 2, 0, 0, 3, b's', b'/', b'#', 1]);
        let mut s = Session::default();
        s.subscribing.push_back(wanted);
        s.subscribing.push_back(Subscribe { topic: "t".into(), qos: 2 });
        s.waiting.push_back(message(0));
        let mut due = subscribe_packet(&Subscribe { topic: "s/#".into(), qos: 1 }, 1);
        due.extend(subscribe_packet(&Subscribe { topic: "t".into(), qos: 2 }, 2));
        due.extend(publish_packet(&message(0), 0, false));
        assert_eq!(s.due(), due);
        // Asked once: nothing is owed after, and nothing is asked again on a new connection.
        assert!(s.due().is_empty() && s.again().is_empty());
    }

    #[test]
    fn a_message_from_the_broker_is_read_whatever_its_quality_of_service_and_properties() {
        assert_eq!(received(0x30, &[0, 1, b't', 0, b'h', b'i']), Some((0, None, &b"t"[..], &b"hi"[..])));
        assert_eq!(received(0x32, &[0, 1, b't', 1, 2, 0, b'h', b'i']), Some((1, Some(258), &b"t"[..], &b"hi"[..])));
        // Retained, sent again, with properties to step over, and an empty payload.
        assert_eq!(received(0x3d, &[0, 1, b't', 0, 9, 2, 1, 0]), Some((2, Some(9), &b"t"[..], &b""[..])));
        for broken in [&[][..], &[0, 5, b't'], &[0, 1, b't'], &[0, 1, b't', 0], &[0, 1, b't', 0, 9, 3, 1]] {
            assert_eq!(received(0x32, broken), None, "{broken:?}");
        }
        assert_eq!(received(0x36, &[0, 1, b't', 0, 9, 0]), None, "a quality of service of 3");
    }

    #[test]
    fn a_received_message_is_written_as_the_python_gateway_logs_it() {
        let at = Duration::from_secs(0);
        let line = |topic: &[u8], payload: &[u8]| received_line(topic, payload, at);
        assert!(line(b"s/t", b" {\"a\": 1}\n").starts_with(r#"{"topic":"s/t","payload":{"a": 1},"event":"mqtt_message_received","level":"info","logger":"dataplane","@timestamp":""#));
        assert!(line(b"s/t", b"21.5").starts_with(r#"{"topic":"s/t","payload":21.5,"#));
        assert!(line(b"s/t", b"plain \"words\"\n").starts_with(r#"{"topic":"s/t","payload":{"raw":"plain \"words\"\n"},"#));
        assert!(line(b"s/t", b"").starts_with(r#"{"topic":"s/t","payload":{"raw":""},"#));
        assert!(line(b"s/t", &[0xff]).starts_with(r#"{"error":"a message that is not UTF-8","event":"mqtt_message_error","#));
        // One line each, and each of them JSON.
        for l in [line(b"a\nb", b"x\ny"), line(b"t", b"{}"), line(b"t", &[0xff])] {
            assert!(l.ends_with("}\n") && l.matches('\n').count() == 1, "{l}");
            assert!(serde_json::from_str::<serde_json::Value>(&l).is_ok(), "{l}");
        }
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
