//! Money in integer cents: splitting an amount without losing a cent, formatting, and a ledger.

use std::collections::BTreeMap;

use crate::error::LedgerError;

/// The currencies `format_cents` knows how to print.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Currency {
    Usd,
    Eur,
    Jpy,
}

/// Split `total_cents` in proportion to `weights` with the largest-remainder method.
///
/// Each share is floored to whole cents, then the cents left over go one at a time to the shares
/// with the largest fractional parts, so `allocate_cents(100, &[1, 1, 1])` is `[34, 33, 33]`.
pub fn allocate_cents(total_cents: i64, weights: &[u32]) -> Result<Vec<i64>, LedgerError> {
    let total_weight: u64 = weights.iter().map(|&w| u64::from(w)).sum();
    if total_weight == 0 {
        return Err(LedgerError::NoWeights);
    }
    let mut shares = Vec::with_capacity(weights.len());
    let mut fractions = Vec::with_capacity(weights.len());
    for (i, &w) in weights.iter().enumerate() {
        let exact = i128::from(total_cents) * i128::from(w);
        let share = exact.div_euclid(i128::from(total_weight));
        shares.push(share as i64);
        fractions.push((exact.rem_euclid(i128::from(total_weight)), i));
    }
    let remainder = total_cents - shares.iter().sum::<i64>();
    fractions.sort_by(|a, b| b.0.cmp(&a.0).then(a.1.cmp(&b.1)));
    for &(_, i) in fractions.iter().take(remainder as usize) {
        shares[i] += 1;
    }
    Ok(shares)
}

/// Print `cents` in `currency`: `$12.34`, `12,34 €`, or whole yen.
pub fn format_cents(cents: i64, currency: Currency) -> String {
    let sign = if cents < 0 { "-" } else { "" };
    let abs = cents.unsigned_abs();
    match currency {
        Currency::Usd => format!("{sign}${}.{:02}", abs / 100, abs % 100),
        Currency::Eur => format!("{sign}{},{:02} €", abs / 100, abs % 100),
        Currency::Jpy => format!("{sign}¥{abs}"),
    }
}

/// Balances by account, never below zero.
#[derive(Debug, Default)]
pub struct Ledger {
    balances: BTreeMap<String, i64>,
}

impl Ledger {
    /// An empty ledger.
    pub fn new() -> Self {
        Self::default()
    }

    /// Add `cents` (negative to withdraw) to `account`; refuses to overdraw it.
    pub fn post(&mut self, account: &str, cents: i64) -> Result<i64, LedgerError> {
        let balance = self.balances.get(account).copied().unwrap_or(0);
        let next = balance + cents;
        if next < 0 {
            return Err(LedgerError::Overdrawn {
                account: account.to_string(),
                short_by: -next,
            });
        }
        self.balances.insert(account.to_string(), next);
        Ok(next)
    }

    /// The balance of `account` (zero when it has never been posted to).
    pub fn balance(&self, account: &str) -> i64 {
        self.balances.get(account).copied().unwrap_or(0)
    }

    /// Move `cents` between two accounts as one operation: both postings or neither.
    pub fn transfer(&mut self, from: &str, to: &str, cents: i64) -> Result<(), LedgerError> {
        self.post(from, -cents)?;
        self.post(to, cents)?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn allocates_the_remainder_to_the_largest_fractions() {
        assert_eq!(allocate_cents(100, &[1, 1, 1]).unwrap(), vec![34, 33, 33]);
    }

    #[test]
    fn allocates_by_weight() {
        assert_eq!(allocate_cents(1000, &[1, 3]).unwrap(), vec![250, 750]);
    }

    #[test]
    fn refuses_zero_weights() {
        assert_eq!(allocate_cents(100, &[0, 0]), Err(LedgerError::NoWeights));
    }

    #[test]
    fn formats_dollars_and_euros() {
        assert_eq!(format_cents(1234, Currency::Usd), "$12.34");
        assert_eq!(format_cents(-5, Currency::Eur), "-0,05 €");
    }

    #[test]
    fn refuses_to_overdraw() {
        let mut ledger = Ledger::new();
        ledger.post("cash", 500).unwrap();
        let err = ledger.post("cash", -700).unwrap_err();
        assert_eq!(err.to_string(), "cash would be overdrawn by 200 cents");
        assert_eq!(ledger.balance("cash"), 500);
    }
}
