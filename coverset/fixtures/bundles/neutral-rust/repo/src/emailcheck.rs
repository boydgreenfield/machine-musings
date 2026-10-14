//! Email address validation shared by signup and invitations.

use crate::error::LedgerError;

/// Check `email` and return its domain, lowercased.
///
/// The rule mirrors the delivery vendor's syntax check: a non-empty local part of
/// `[A-Za-z0-9._+-]`, one `@`, and a domain of at least two labels whose last label is two or
/// more letters.
pub fn validate_email(email: &str) -> Result<String, LedgerError> {
    let bad = || LedgerError::BadEmail(email.to_string());
    let (local, domain) = email.split_once('@').ok_or_else(bad)?;
    let local_ok = !local.is_empty()
        && local
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || "._+-".contains(c));
    if !local_ok {
        return Err(bad());
    }
    let labels: Vec<&str> = domain.split('.').collect();
    let tld = labels.last().copied().unwrap_or("");
    if labels.len() < 2 || tld.len() < 2 || !tld.chars().all(|c| c.is_ascii_alphabetic()) {
        return Err(bad());
    }
    if labels
        .iter()
        .any(|l| l.is_empty() || !l.chars().all(|c| c.is_ascii_alphanumeric() || c == '-'))
    {
        return Err(bad());
    }
    Ok(domain.to_ascii_lowercase())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn returns_the_lowercased_domain() {
        assert_eq!(validate_email("Ada@Example.COM").unwrap(), "example.com");
    }

    #[test]
    fn rejects_a_missing_at_sign() {
        assert!(validate_email("ada.example.com").is_err());
    }
}
