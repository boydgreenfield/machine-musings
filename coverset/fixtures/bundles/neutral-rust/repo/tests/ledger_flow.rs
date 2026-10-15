//! Integration tests: the public API used the way a billing job would.

use ledger::{allocate_cents, format_cents, merge_intervals, parse_duration, validate_email};
use ledger::{Currency, Ledger, LedgerError};

#[test]
fn splits_an_invoice_and_posts_every_share() {
    let shares = allocate_cents(10_001, &[2, 1, 1]).unwrap();
    assert_eq!(shares.iter().sum::<i64>(), 10_001);
    let mut ledger = Ledger::new();
    for (i, cents) in shares.iter().enumerate() {
        ledger.post(&format!("member-{i}"), *cents).unwrap();
    }
    assert_eq!(ledger.balance("member-0"), 5_001);
    assert_eq!(format_cents(ledger.balance("member-1"), Currency::Jpy), "¥2500");
}

#[test]
fn a_failed_transfer_reports_the_shortfall() {
    let mut ledger = Ledger::new();
    ledger.post("ops", 100).unwrap();
    let err = ledger.transfer("ops", "payroll", 250).unwrap_err();
    assert_eq!(
        err,
        LedgerError::Overdrawn {
            account: "ops".into(),
            short_by: 150
        }
    );
}

#[test]
fn billing_windows_merge_and_durations_parse() {
    let windows = merge_intervals(vec![(0, 3_600), (1_800, parse_duration("2h").unwrap() as u32)]);
    assert_eq!(windows, vec![(0, 7_200)]);
    assert!(parse_duration("5x").is_err());
}

#[test]
fn signup_rejects_a_bad_domain() {
    assert!(matches!(validate_email("bob@localhost"), Err(LedgerError::BadEmail(_))));
    assert_eq!(validate_email("bob+bills@mail.acme.io").unwrap(), "mail.acme.io");
}
