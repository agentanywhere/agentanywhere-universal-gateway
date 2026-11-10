//! Circuit breaker for the backend, with the Python gateway's rules.
//!
//! A failure is a request that got no response (refused, reset, timed out); a 5xx answer is not
//! one. After `threshold` failures in a row the breaker opens and requests are refused without
//! being sent. Once `timeout_secs` have passed it goes half-open with the count back at zero:
//! requests are sent again, the first success closes it, and `threshold` more failures reopen it.
//!
//! While it is closed with no failures counted (the normal case) a check is one atomic load.
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant};

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Config {
    pub enabled: bool,
    pub threshold: u32,
    pub timeout_secs: u64,
}

impl Default for Config {
    /// The gateway's shipped defaults.
    fn default() -> Self {
        Config { enabled: true, threshold: 5, timeout_secs: 60 }
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum State {
    Closed,
    Open,
    HalfOpen,
}

impl State {
    pub fn name(self) -> &'static str {
        match self {
            State::Closed => "closed",
            State::Open => "open",
            State::HalfOpen => "half-open",
        }
    }
}

/// The breaker opened, or closed again: what the control plane announces by webhook and alert.
#[derive(Debug, PartialEq)]
pub struct Transition {
    pub state: State,
    pub previous: State,
    pub failures: u32,
}

struct Inner {
    state: State,
    failures: u32,
    opened: Option<Instant>,
}

pub struct Breaker {
    /// True while closed with no failures counted: nothing to check and nothing to reset.
    quiet: AtomicBool,
    inner: Mutex<Inner>,
}

impl Breaker {
    pub fn new() -> Breaker {
        Breaker {
            quiet: AtomicBool::new(true),
            inner: Mutex::new(Inner { state: State::Closed, failures: 0, opened: None }),
        }
    }

    /// May a request be sent to the backend at `now`?
    pub fn allow(&self, cfg: &Config, now: Instant) -> bool {
        if !cfg.enabled || self.quiet.load(Ordering::Relaxed) {
            return true;
        }
        let mut g = self.inner.lock().unwrap();
        if g.state != State::Open {
            return true;
        }
        let waited = g.opened.map(|at| now.saturating_duration_since(at)).unwrap_or(Duration::MAX);
        if waited > Duration::from_secs(cfg.timeout_secs) {
            g.state = State::HalfOpen;
            g.failures = 0;
            return true;
        }
        false
    }

    /// The backend answered.
    pub fn success(&self, cfg: &Config) -> Option<Transition> {
        if !cfg.enabled || self.quiet.load(Ordering::Relaxed) {
            return None;
        }
        let mut g = self.inner.lock().unwrap();
        let closed = (g.state == State::HalfOpen).then(|| {
            g.state = State::Closed;
            Transition { state: State::Closed, previous: State::HalfOpen, failures: g.failures }
        });
        g.failures = 0;
        // A request already in flight when the breaker opened can succeed; it stays open, as in Python.
        self.quiet.store(g.state == State::Closed, Ordering::Relaxed);
        closed
    }

    /// The backend did not answer.
    pub fn failure(&self, cfg: &Config, now: Instant) -> Option<Transition> {
        if !cfg.enabled {
            return None;
        }
        let mut g = self.inner.lock().unwrap();
        self.quiet.store(false, Ordering::Relaxed);
        let previous = g.state;
        g.failures += 1;
        if g.failures < cfg.threshold {
            return None;
        }
        g.state = State::Open;
        g.opened = Some(now);
        (previous != State::Open).then_some(Transition { state: State::Open, previous, failures: g.failures })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const CFG: Config = Config { enabled: true, threshold: 3, timeout_secs: 10 };

    fn opened(now: Instant) -> Breaker {
        let b = Breaker::new();
        assert_eq!(b.failure(&CFG, now), None);
        assert_eq!(b.failure(&CFG, now), None);
        assert_eq!(
            b.failure(&CFG, now),
            Some(Transition { state: State::Open, previous: State::Closed, failures: 3 })
        );
        b
    }

    #[test]
    fn opens_after_the_threshold_and_refuses() {
        let now = Instant::now();
        let b = opened(now);
        assert!(!b.allow(&CFG, now));
        assert!(!b.allow(&CFG, now + Duration::from_secs(10)), "the timeout must have passed, not just been reached");
    }

    #[test]
    fn a_success_resets_the_count() {
        let now = Instant::now();
        let b = Breaker::new();
        b.failure(&CFG, now);
        b.failure(&CFG, now);
        assert_eq!(b.success(&CFG), None);
        assert_eq!(b.failure(&CFG, now), None, "the count started again from zero");
        assert!(b.allow(&CFG, now));
    }

    #[test]
    fn half_open_after_the_timeout_and_a_success_closes_it() {
        let now = Instant::now();
        let b = opened(now);
        let later = now + Duration::from_secs(11);
        assert!(b.allow(&CFG, later));
        assert!(b.allow(&CFG, later), "half-open lets every request through, as the Python gateway does");
        assert_eq!(
            b.success(&CFG),
            Some(Transition { state: State::Closed, previous: State::HalfOpen, failures: 0 })
        );
        assert_eq!(b.success(&CFG), None);
    }

    #[test]
    fn half_open_reopens_only_after_the_threshold_again() {
        let now = Instant::now();
        let b = opened(now);
        let later = now + Duration::from_secs(11);
        assert!(b.allow(&CFG, later));
        assert_eq!(b.failure(&CFG, later), None);
        assert_eq!(b.failure(&CFG, later), None);
        assert_eq!(
            b.failure(&CFG, later),
            Some(Transition { state: State::Open, previous: State::HalfOpen, failures: 3 })
        );
        assert!(!b.allow(&CFG, later));
    }

    #[test]
    fn a_late_failure_while_open_restarts_the_wait_without_a_second_announcement() {
        let now = Instant::now();
        let b = opened(now);
        let later = now + Duration::from_secs(8);
        assert_eq!(b.failure(&CFG, later), None);
        assert!(!b.allow(&CFG, now + Duration::from_secs(11)));
        assert!(b.allow(&CFG, later + Duration::from_secs(11)));
    }

    #[test]
    fn disabled_never_opens() {
        let cfg = Config { enabled: false, ..CFG };
        let now = Instant::now();
        let b = Breaker::new();
        for _ in 0..10 {
            assert_eq!(b.failure(&cfg, now), None);
        }
        assert!(b.allow(&cfg, now));
    }
}
