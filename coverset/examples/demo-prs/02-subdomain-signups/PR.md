# Allow signups from company subdomains

Prospects on company subdomains such as `team.example.com` could not sign up: `validate_email`
rejected any domain with more than one label before the TLD, although our email vendor delivers to
those addresses. This widens the pattern to match the vendor's rule and makes the existing-customer
check case-insensitive on both sides.

- `validate_email` accepts domains with any number of labels and still returns the domain, lowercased.
- `check_if_new_user_signup_allowed` lowercases the customer domains before comparing.
- `tests/test_signup.py` is consolidated into one table of cases; all tests pass and patch coverage is 100%.
