//! Tenant quotas that hold across both planes.
//!
//! The Python gateway counts a tenant's requests in the database, per minute and per hour, and
//! refuses a request when `used + 1` would pass the limit. The data plane keeps the same two
//! windows in memory: `base` is the database's count as of the control plane's last answer, and
//! `unacked` is what this process admitted since and the database has not yet been told. Each
//! report carries the requests completed per tenant; once it is acknowledged they move from
//! `unacked` to `base`, and the next snapshot answer replaces `base` with the database's total,
//! which by then includes them and whatever the Python gateway served itself.
//!
//! A request in flight counts against the quota here (it does not yet in the Python gateway), so
//! under concurrency this side is the stricter one, never the looser.
use hyper::header::HeaderValue;
use std::sync::{Arc, Mutex};

pub struct Tenant {
    pub id: Arc<str>,
    /// Requests allowed per minute and per hour; `None` is no limit.
    pub minute: Option<u64>,
    pub hour: Option<u64>,
    /// `{"tenant_id":...,"quota":{...}}`: what a refusal says after the limit that was hit.
    pub details_json: String,
    /// The `X-Tenant-ID` response header, when the id can be one.
    pub header: Option<HeaderValue>,
    /// Shared across snapshot updates so a sync never resets the tenant's counts.
    pub usage: Arc<Usage>,
}

#[derive(Default)]
struct Win {
    /// Epoch seconds the window started at.
    start: u64,
    base: u64,
    unacked: u64,
}

impl Win {
    fn roll(&mut self, current: u64) {
        if current > self.start {
            *self = Win { start: current, base: 0, unacked: 0 };
        }
    }

    fn acked(&mut self, start: u64, n: u64) {
        if start == self.start {
            self.base += n;
            self.unacked = self.unacked.saturating_sub(n);
        }
    }

    fn set_base(&mut self, start: u64, used: u64) {
        if start > self.start {
            *self = Win { start, base: used, unacked: 0 };
        } else if start == self.start {
            self.base = used;
        }
    }
}

/// Requests completed for a tenant that were admitted in one minute.
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct Slot {
    /// Epoch seconds of the minute's start.
    pub minute: u64,
    pub requests: u64,
    pub errors: u64,
    pub bytes_out: u64,
}

/// Minutes of undelivered usage kept per tenant while the control plane is unreachable.
const MAX_PENDING: usize = 120;

#[derive(Default)]
struct Inner {
    minute: Win,
    hour: Win,
    pending: Vec<Slot>,
}

#[derive(Default)]
pub struct Usage(Mutex<Inner>);

/// The quota that refused a request.
#[derive(Debug, PartialEq)]
pub struct Over {
    pub window: &'static str,
    pub limit: u64,
    pub retry_after: u32,
}

/// An admitted request. Dropped without `complete` (the client went away), it gives its place back.
pub struct Ticket<'a> {
    usage: &'a Usage,
    minute: u64,
    counted: bool,
    done: bool,
}

fn hour_of(minute: u64) -> u64 {
    minute - minute % 3600
}

impl Usage {
    /// Admit one request at `now` (epoch seconds), or say which quota is used up: the minute's
    /// is checked first, as in the Python gateway.
    pub fn admit(&self, minute: Option<u64>, hour: Option<u64>, now: u64) -> Result<Ticket<'_>, Over> {
        if minute.is_none() && hour.is_none() {
            return Ok(Ticket { usage: self, minute: now - now % 60, counted: false, done: false });
        }
        let mut g = self.0.lock().unwrap();
        g.minute.roll(now - now % 60);
        g.hour.roll(hour_of(now));
        if let Some(limit) = minute {
            if g.minute.base + g.minute.unacked + 1 > limit {
                return Err(Over { window: "minute", limit, retry_after: 60 });
            }
        }
        if let Some(limit) = hour {
            if g.hour.base + g.hour.unacked + 1 > limit {
                return Err(Over { window: "hour", limit, retry_after: 3600 });
            }
        }
        g.minute.unacked += 1;
        g.hour.unacked += 1;
        Ok(Ticket { usage: self, minute: g.minute.start, counted: true, done: false })
    }

    /// Usage not yet reported, oldest minute first.
    pub fn take(&self) -> Vec<Slot> {
        std::mem::take(&mut self.0.lock().unwrap().pending)
    }

    /// The control plane has added `slots` to the database.
    pub fn acked(&self, slots: &[Slot]) {
        let mut g = self.0.lock().unwrap();
        for s in slots {
            g.minute.acked(s.minute, s.requests);
            g.hour.acked(hour_of(s.minute), s.requests);
        }
    }

    /// A report was not delivered: its usage goes into the next one.
    pub fn restore(&self, slots: Vec<Slot>) {
        let mut g = self.0.lock().unwrap();
        for s in slots {
            add(&mut g.pending, s);
        }
        g.pending.sort_by_key(|s| s.minute);
        let extra = g.pending.len().saturating_sub(MAX_PENDING);
        g.pending.drain(..extra);
    }

    /// The database's totals for the windows starting at `minute_start` and `hour_start`.
    pub fn set_base(&self, minute_start: u64, hour_start: u64, minute_used: u64, hour_used: u64) {
        let mut g = self.0.lock().unwrap();
        g.minute.set_base(minute_start, minute_used);
        g.hour.set_base(hour_start, hour_used);
    }
}

fn add(pending: &mut Vec<Slot>, s: Slot) {
    match pending.iter_mut().find(|p| p.minute == s.minute) {
        Some(p) => {
            p.requests += s.requests;
            p.errors += s.errors;
            p.bytes_out += s.bytes_out;
        }
        None => {
            if pending.len() >= MAX_PENDING {
                pending.remove(0);
            }
            pending.push(s);
        }
    }
}

impl Ticket<'_> {
    /// The request was answered: it is usage, whatever the answer was.
    pub fn complete(mut self, status: u16, bytes_out: u64) {
        self.done = true;
        let slot = Slot { minute: self.minute, requests: 1, errors: u64::from(status >= 400), bytes_out };
        add(&mut self.usage.0.lock().unwrap().pending, slot);
    }
}

impl Drop for Ticket<'_> {
    fn drop(&mut self) {
        if self.done || !self.counted {
            return;
        }
        let mut g = self.usage.0.lock().unwrap();
        if g.minute.start == self.minute {
            g.minute.unacked = g.minute.unacked.saturating_sub(1);
        }
        if g.hour.start == hour_of(self.minute) {
            g.hour.unacked = g.hour.unacked.saturating_sub(1);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const T: u64 = 1_800_000_000 - 1_800_000_000 % 3600; // the start of an hour

    #[test]
    fn a_quota_admits_up_to_the_limit_then_names_the_window() {
        let u = Usage::default();
        for _ in 0..3 {
            u.admit(Some(3), Some(100), T + 5).unwrap().complete(200, 10);
        }
        let over = u.admit(Some(3), Some(100), T + 59).err().unwrap();
        assert_eq!(over, Over { window: "minute", limit: 3, retry_after: 60 });
        assert!(u.admit(Some(3), Some(100), T + 60).is_ok(), "a new minute starts empty");
    }

    #[test]
    fn the_hour_holds_across_minutes() {
        let u = Usage::default();
        u.admit(Some(5), Some(2), T).unwrap().complete(200, 0);
        u.admit(Some(5), Some(2), T + 60).unwrap().complete(200, 0);
        let over = u.admit(Some(5), Some(2), T + 120).err().unwrap();
        assert_eq!(over, Over { window: "hour", limit: 2, retry_after: 3600 });
        assert!(u.admit(Some(5), Some(2), T + 3600).is_ok());
    }

    #[test]
    fn a_request_that_never_completes_gives_its_place_back() {
        let u = Usage::default();
        drop(u.admit(Some(1), None, T).unwrap());
        assert!(u.admit(Some(1), None, T).is_ok());
    }

    #[test]
    fn completed_requests_are_reported_per_admission_minute() {
        let u = Usage::default();
        u.admit(None, None, T + 1).unwrap().complete(200, 100);
        u.admit(None, None, T + 2).unwrap().complete(502, 50);
        u.admit(None, None, T + 61).unwrap().complete(200, 1);
        assert_eq!(
            u.take(),
            vec![
                Slot { minute: T, requests: 2, errors: 1, bytes_out: 150 },
                Slot { minute: T + 60, requests: 1, errors: 0, bytes_out: 1 },
            ]
        );
        assert!(u.take().is_empty());
    }

    #[test]
    fn what_the_database_holds_counts_against_the_quota() {
        let u = Usage::default();
        u.set_base(T, T, 2, 2);
        assert!(u.admit(Some(3), None, T + 1).is_ok());
        let u = Usage::default();
        u.set_base(T, T, 3, 3);
        assert!(u.admit(Some(3), None, T + 1).is_err(), "the other plane used the quota up");
        assert!(u.admit(Some(3), None, T + 60).is_ok());
    }

    #[test]
    fn acknowledged_usage_is_counted_once() {
        let u = Usage::default();
        for _ in 0..2 {
            u.admit(Some(5), None, T).unwrap().complete(200, 0);
        }
        let slots = u.take();
        u.acked(&slots);
        // The database now says 2, and one more was served by the Python gateway.
        u.set_base(T, T, 3, 3);
        let _in_flight = [u.admit(Some(5), None, T + 1).unwrap(), u.admit(Some(5), None, T + 1).unwrap()];
        assert!(u.admit(Some(5), None, T + 1).is_err());
    }

    #[test]
    fn an_answer_from_before_the_report_does_not_free_the_quota() {
        let u = Usage::default();
        for _ in 0..3 {
            u.admit(Some(3), None, T).unwrap().complete(200, 0);
        }
        // Not yet reported: the database still says 0.
        u.set_base(T, T, 0, 0);
        assert!(u.admit(Some(3), None, T + 1).is_err());
    }

    #[test]
    fn undelivered_usage_goes_into_the_next_report() {
        let u = Usage::default();
        u.admit(Some(5), None, T).unwrap().complete(200, 7);
        let slots = u.take();
        u.admit(Some(5), None, T).unwrap().complete(200, 3);
        u.restore(slots);
        assert_eq!(u.take(), vec![Slot { minute: T, requests: 2, errors: 0, bytes_out: 10 }]);
    }
}
