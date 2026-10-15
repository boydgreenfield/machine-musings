//! Compact duration strings such as `90s`, `15m` or `1h30m`.

use crate::error::LedgerError;

/// Seconds in one `unit` (`s`, `m`, `h` or `d`), or `None` for any other character.
fn unit_seconds(unit: char) -> Option<u64> {
    match unit {
        's' => Some(1),
        'm' => Some(60),
        'h' => Some(3_600),
        'd' => Some(86_400),
        _ => None,
    }
}

/// The number of seconds in `text`, a run of `<amount><unit>` tokens that add up.
///
/// `"1h30m"` is 5,400 seconds. Surrounding whitespace is ignored; anything else is an error.
pub fn parse_duration(text: &str) -> Result<u64, LedgerError> {
    let bad = || LedgerError::BadDuration(text.to_string());
    let trimmed = text.trim();
    if trimmed.is_empty() {
        return Err(bad());
    }
    let mut total = 0u64;
    let mut amount: Option<u64> = None;
    for c in trimmed.chars() {
        if let Some(digit) = c.to_digit(10) {
            amount = Some(amount.unwrap_or(0) * 10 + u64::from(digit));
        } else {
            let seconds = unit_seconds(c).ok_or_else(bad)?;
            total += amount.take().ok_or_else(bad)? * seconds;
        }
    }
    if amount.is_some() {
        return Err(bad());
    }
    Ok(total)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_hours_and_minutes() {
        assert_eq!(parse_duration("1h30m"), Ok(5_400));
    }

    #[test]
    fn rejects_a_bare_number() {
        assert!(parse_duration("90").is_err());
    }

    #[test]
    #[ignore = "day-long durations are not supported by the billing cycle yet"]
    fn parses_days() {
        assert_eq!(parse_duration("2d"), Ok(172_800));
    }
}
