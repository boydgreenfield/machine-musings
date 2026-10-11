# pytest-coverset

The in-sandbox half of [coverset](../../README.md): a pytest plugin that records per-test coverage, the shapes of the arguments each function received, runs tests in the order the control plane asks for, and streams results as JSON lines. It also ships `coverset-ci wait`, a stdlib-only command for CI jobs that want to wait for a coverset verdict.
