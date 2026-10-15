//! A small ledger library, the Rust twin of coverset's Python demo app.
//!
//! `coverset rust run --crate examples/rust-app` runs its tests one process per test and writes a
//! neutral bundle under `.coverset/neutral` (see `docs/rust.md`).

pub mod durations;
pub mod emailcheck;
pub mod error;
pub mod intervals;
pub mod money;

pub use durations::parse_duration;
pub use emailcheck::validate_email;
pub use error::LedgerError;
pub use intervals::merge_intervals;
pub use money::{allocate_cents, format_cents, Currency, Ledger};
