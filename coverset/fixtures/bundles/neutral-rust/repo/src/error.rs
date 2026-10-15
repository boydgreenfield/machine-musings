//! The one error type every fallible function of the ledger returns.

use std::fmt;

/// Why a ledger operation was refused.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LedgerError {
    /// `allocate_cents` was given no weights, or weights that sum to zero.
    NoWeights,
    /// A duration string held no `<amount><unit>` token, or an unknown unit.
    BadDuration(String),
    /// An email address failed the syntax check.
    BadEmail(String),
    /// A posting would take an account below zero.
    Overdrawn { account: String, short_by: i64 },
}

impl fmt::Display for LedgerError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            LedgerError::NoWeights => write!(f, "no weights to allocate over"),
            LedgerError::BadDuration(text) => write!(f, "not a duration: {text:?}"),
            LedgerError::BadEmail(text) => write!(f, "malformed email address: {text:?}"),
            LedgerError::Overdrawn { account, short_by } => {
                write!(f, "{account} would be overdrawn by {short_by} cents")
            }
        }
    }
}

impl std::error::Error for LedgerError {}
