# Demo pull requests

Each `NN-slug/` directory is one pull request against the `examples/demo-app` tree.
`scripts/seed_demo.py` turns it into the branch `pr-N-slug` with a single commit on top of `main`:

- `PR.md`: the first line is the title (`# Title`), the rest is the pull request body.
- `files/`: copied over the demo-app tree, path for path (new files are added, existing ones replaced).
- `DELETE` (optional): demo-app paths to remove, one per line.

| Branch | Title | Outcome |
|---|---|---|
| `pr-1-partial-refunds` | Support partial refunds | `test_refund_partial` fails: `allocate_cents` loses cents on a negative total |
| `pr-2-subdomain-signups` | Allow signups from company subdomains | every test passes with 100% patch coverage, and no test passes a multi-label domain |
