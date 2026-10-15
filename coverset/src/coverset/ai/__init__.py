"""Advisory AI review of changed functions behind a pluggable client.

``build_review_bundle`` turns a run's redacted facts into a :class:`ReviewBundle`;
``get_review_client`` picks the client for ``COVERSET_AI`` (off, fake or anthropic);
``review_changes`` reviews one function per call and returns :class:`ReviewFindingRow` objects for
the ``review_findings`` table. Findings are labelled fixture, heuristic or anthropic and never change
pass/fail or test-decided invariant statuses.
"""
