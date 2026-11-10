//! Fixed-window counters with no locks.
//!
//! Same rule as the Python gateway's in-process limiter: a window starts at the first
//! request after the previous one expired and lasts `window` seconds; a request is
//! allowed while the count is below the limit. One 64-bit atomic holds the window's
//! expiry (high 32 bits, epoch seconds) and the count (low 32 bits), so a check is one
//! compare-and-swap loop.
use std::sync::atomic::{AtomicU64, Ordering};

pub struct Window(AtomicU64);

impl Window {
    pub const fn new() -> Self {
        Window(AtomicU64::new(0))
    }

    /// True if the request is within `limit` for a window of `window_secs` seconds at `now`.
    pub fn allow(&self, limit: u32, window_secs: u32, now: u32) -> bool {
        loop {
            let cur = self.0.load(Ordering::Relaxed);
            let (expiry, count) = ((cur >> 32) as u32, cur as u32);
            let (expiry, count) = if now >= expiry { (now + window_secs, 0) } else { (expiry, count) };
            if count >= limit {
                // Keep the (possibly renewed) window so a burst of denials does not reset it.
                let keep = ((expiry as u64) << 32) | count as u64;
                if keep != cur {
                    let _ = self.0.compare_exchange_weak(cur, keep, Ordering::Relaxed, Ordering::Relaxed);
                }
                return false;
            }
            let next = ((expiry as u64) << 32) | (count + 1) as u64;
            if self.0.compare_exchange_weak(cur, next, Ordering::Relaxed, Ordering::Relaxed).is_ok() {
                return true;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn allows_up_to_the_limit_then_denies() {
        let w = Window::new();
        assert!((0..3).all(|_| w.allow(3, 60, 1000)));
        assert!(!w.allow(3, 60, 1000));
        assert!(!w.allow(3, 60, 1059));
    }

    #[test]
    fn a_new_window_starts_after_expiry() {
        let w = Window::new();
        assert!(w.allow(1, 60, 1000));
        assert!(!w.allow(1, 60, 1030));
        assert!(w.allow(1, 60, 1060));
    }

    #[test]
    fn a_zero_limit_denies_everything() {
        assert!(!Window::new().allow(0, 60, 1000));
    }

    #[test]
    fn concurrent_checks_never_exceed_the_limit() {
        use std::sync::Arc;
        let w = Arc::new(Window::new());
        let handles: Vec<_> = (0..8)
            .map(|_| {
                let w = w.clone();
                std::thread::spawn(move || (0..1000).filter(|_| w.allow(500, 60, 2000)).count())
            })
            .collect();
        let allowed: usize = handles.into_iter().map(|h| h.join().unwrap()).sum();
        assert_eq!(allowed, 500);
    }
}
