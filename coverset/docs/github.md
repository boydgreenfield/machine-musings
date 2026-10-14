# GitHub integration

coverset hears about pull requests and pushes through webhooks, runs the tests itself, and reports
back on the pull request. Nothing in this integration has been exercised against github.com: it is
verified against recorded, signed webhook fixtures (`fixtures/webhooks/`), a fake forge that
records every call, and a mocked GitHub API. Treat the first real installation as a test.

There are two ways to connect a repository, and they combine:

| | GitHub App | Token mode | Thin Actions step |
|---|---|---|---|
| Triggers runs from webhooks | yes | yes (repository webhook) | no (it only waits) |
| Check run with annotations and buttons | yes | no | no |
| Commit status | no (the check run replaces it) | yes | no |
| Sticky PR comment | yes | yes | no |
| Can be a required check | yes, bound to the App | yes, by status context | yes, as the job |
| Fork PRs can be approved with a button | yes | no (an admin approves in coverset) | n/a |

## 1. Create the GitHub App (manifest flow)

1. Make coverset reachable from GitHub (see [Local tunnels](#local-tunnels)): set
   `COVERSET_ADMIN_TOKEN` (at least 32 characters) first, then set `COVERSET_PUBLIC_URL` to the
   public URL. Set `COVERSET_SECRET_KEY` (at least 32 random characters, `openssl rand -hex 32`, the
   same on every replica; coverset refuses to start with a shorter one): the App's credentials are
   stored in the database encrypted under it, and
   without it `/setup` disables its button and says so. coverset refuses to start with a public URL that is not loopback (or an RFC 6761
   name such as `*.test`) and no admin token, because the tunnel would hand every internet caller
   the operator's open-mode access. GitHub refuses webhook URLs on loopback addresses, and the
   `/setup` page warns when `COVERSET_PUBLIC_URL` is one.
2. Start `coverset serve` and open `/setup` (or `/setup?org=<organization>` for an organization).
3. Press **Create the GitHub App**. The page posts this manifest to
   `https://github.com/settings/apps/new?state=<random>`; the `state` is kept in your session and
   checked when GitHub sends you back.

   | Manifest field | Value |
   |---|---|
   | `public` | `false` (only your account or organization can install it) |
   | `hook_attributes.url` | `COVERSET_PUBLIC_URL/webhooks/github` |
   | `redirect_url` | `COVERSET_PUBLIC_URL/setup/github/callback` |
   | `callback_urls` | `[COVERSET_PUBLIC_URL/auth/github/callback]`: the OAuth callback of "Sign in with GitHub" ([docs/auth.md](auth.md)) |
   | `request_oauth_on_install` | `false`: installing the App never starts a sign-in |
   | `default_permissions` | `checks: write`, `contents: read`, `pull_requests: write`, `issues: write`, `metadata: read` |
   | `default_events` | `pull_request`, `push`, `check_run`, `check_suite`, `repository` |

   `installation` and `installation_repositories` events reach every App without subscribing.
   `repository` (`privatized`/`publicized`) keeps the stored privacy current, so a repository made
   private stops answering anonymous `/v1` callers at once; it needs only `metadata: read`.
4. GitHub creates the App and redirects to `/setup/github/callback?code=...&state=...`. coverset
   checks the `state`, moves the one-time code into your session and redirects to the code-free
   `/setup/github/finish` (so the code never stays in a logged or shared URL; access-log lines for
   the callback are redacted too). There it exchanges the code with
   `POST /app-manifests/{code}/conversions` and stores the result as one row of the database's
   `settings` table (`github_app`): the App id, client id, client secret, webhook secret, private
   key, owner, slug and HTML URL and when it was stored, as one JSON document sealed with
   AES-256-GCM under a key derived from `COVERSET_SECRET_KEY` ([security.md](security.md#5-secrets)).
   No file is written. The success page shows the App's name, the installation link and
   "Credentials stored"; the action is audited (`github_app_stored`).

   **No restart.** Every replica re-checks the row at most every 5 s (one indexed read of its
   `updated_at`; it is decrypted again only when that changed), so the forge adapter, webhook
   verification and "Sign in with GitHub" switch to the new App within seconds everywhere; the
   replica that ran `/setup` switches at once, and an orchestrator worker or a CLI run re-checks
   the row before it refreshes a mirror or posts a check, so runs see the App without a restart
   too. With no `COVERSET_ALLOWED_OWNERS`, only the App's own owner is accepted (an explicit list
   wins).

   The client id and secret are what "Sign in with GitHub" uses: the login page offers it as soon as
   the App is stored, and collaborators who sign in see the repositories GitHub grants them
   ([docs/auth.md](auth.md)). Add your own login to `COVERSET_ADMINS` to be an instance admin
   when signed in with GitHub (the admin token always is). An App created before the manifest
   carried `callback_urls` needs `COVERSET_PUBLIC_URL/auth/github/callback` added under the App's
   settings on GitHub ("Callback URL").

   If an App is already stored the finish step answers 409. Replacing it is a `POST` with
   `overwrite=1` and the CSRF token, and it always needs an admin session (sign in with
   `COVERSET_ADMIN_TOKEN`); in open mode press **Forget the App** on `/setup` first (a `POST` with the
   CSRF token, accepted from an admin session or the local operator, audited as
   `github_app_forgotten`; the App itself stays on GitHub until you delete it there). Of two
   setups finishing at once, the insert lets exactly one store its App; the other page says which
   App GitHub created anyway, so it can be deleted. Without an admin session the callback, finish
   and forget steps are accepted only from the local operator: a loopback client address, a loopback `Host` and no
   proxy headers (`Via`, `True-Client-IP`, `CF-Connecting-IP`, or any header whose name contains
   `forwarded`, `client-ip`, `real-ip` or `proxy`, such as `X-Forwarded-For`, `X-Client-IP`,
   `Fastly-Client-IP` or `X-Original-Forwarded-For`). A tunnel also connects from 127.0.0.1, so the
   address alone proves nothing. A wrong or reused `state` is a 400, and a code is used once.
5. Install the App on the repositories coverset should watch (the success page links to
   `https://github.com/apps/<slug>/installations/new`).

### Credentials from the environment instead

Operators who keep secrets in their platform's store can skip the stored App and set
`COVERSET_GITHUB_APP_ID`, `COVERSET_GITHUB_APP_CLIENT_ID`, `COVERSET_GITHUB_APP_CLIENT_SECRET`,
`COVERSET_GITHUB_APP_PRIVATE_KEY` (the PEM text itself; a value whose line breaks a secret store
flattened to literal `\n` is accepted) and `COVERSET_GITHUB_WEBHOOK_SECRET`, with
`COVERSET_ALLOWED_OWNERS` (required in this case). The GitHub credentials then come from exactly one
place: while **any** of those names, or `COVERSET_GITHUB_TOKEN` (token mode, below), is set, the
stored row is ignored and `/setup` is read-only: its page lists the names it found, its button is
disabled, and the finish and forget steps answer 409 saying so. Changing environment credentials
needs a restart, as any environment change does.

App mode refuses to start without `COVERSET_ALLOWED_OWNERS`. Events for any other owner (or, with
`COVERSET_ALLOWED_REPOS`, any other repository) are stored as `ignored_owner` without their payload,
answered 202, and never create a repository or a run.

### Make coverset a required check

The verdict is a check run named `coverset` created by the App. Require it from that App so no
other integration can satisfy it:

* repository rulesets: a "Require status checks to pass" rule with
  `{"context": "coverset", "integration_id": <App id>}`;
* classic branch protection: `required_status_checks.checks[]` with
  `{"context": "coverset", "app_id": <App id>}`.

Untrusted pull requests first get a separate, neutral check named `coverset: awaiting approval`
(see below). Because it has a different name, the required `coverset` check stays pending until a
maintainer approves and the run finishes.

### What the App posts

* `coverset` check run: created `in_progress` when a run starts and completed with the verdict.
  The summary repeats the sticky comment; failing tests become `failure` annotations, sent at most
  50 per PATCH (GitHub's limit) with title and summary repeated on each. Buttons: **Re-run
  failed** and **Re-run all** (labels stay under GitHub's 20-character limit).
* One sticky PR comment, edited in place. coverset stores its id; if that id is gone it scans the
  PR's comments for the hidden marker `<!-- coverset:sticky -->`, but only in comments written by
  this App (`performed_via_github_app.id == COVERSET_GITHUB_APP_ID`) or, in token mode, by the
  token's user, so a marker pasted by someone else is never edited. Every interpolated string is
  markdown-escaped, test ids sit in code spans with a fence longer than any backtick run inside,
  `@` mentions are neutralised, and there are no images or user-controlled HTML.

All writes are spaced by a token bucket (about 100 a minute, short bursts allowed) to stay inside
GitHub's secondary rate limit, and every API request pins `X-GitHub-Api-Version: 2022-11-28`.
Server errors are retried (up to three times) only for `GET` and `PATCH`: a 502 on a `POST` that
GitHub actually committed must not create a second comment or check run. A rate-limited request is
retried once, and only when the reset is at most 60 s away.

## 2. Token mode

Without an App, coverset can post a commit status and the sticky comment:

1. Create a fine-grained personal access token limited to the watched repositories with
   *Commit statuses: read and write*, *Pull requests: read and write*, *Issues: read and write* and
   *Contents: read*.
2. Set `COVERSET_GITHUB_TOKEN` (at least 16 characters), `COVERSET_ALLOWED_OWNERS` and a random
   `COVERSET_GITHUB_WEBHOOK_SECRET` of at least 16 characters.
3. Add a repository webhook (Settings → Webhooks): payload URL `COVERSET_PUBLIC_URL/webhooks/github`,
   content type `application/json`, the same secret, events *Pull requests* and *Pushes*.

Token mode has no check runs, so there are no buttons: untrusted pull requests cannot be approved
from GitHub. Unless `COVERSET_UNTRUSTED_PR_POLICY` is set, token mode therefore runs them at once
with the network blocked (`run_blocked_network`, logged at startup). Set it to `skip` to never run
them, or explicitly to `approve`: they then wait behind a pending `coverset: awaiting approval`
status until an admin approves with `POST /runs/<run_id>/approve` (admin bearer, or the login
cookie plus the CSRF token), which also turns that status green and queues the run.

The verdict status `coverset` is green only for `success`: `neutral` (an errored run, no tests, an
exhausted budget), `cancelled` and `timed_out` post `error`, so a required status never passes by
accident.

## Choosing the adapter

`COVERSET_FORGE` picks the adapter: `auto` (default: the GitHub adapter when there is an App,
stored by `/setup` or from the environment, or `COVERSET_GITHUB_TOKEN` is set, else the fake
forge), `github` or `fake`. The app's adapter is rebuilt when the stored App changes
(`forge/registry.py`, `LiveForge`), so storing or forgetting the App switches between the fake
forge and App mode without a restart. The fake forge parses the same GitHub-shaped payloads and records every call it would have
made in the `forge_calls` table (`coverset forge calls`, and `/forge/outbox` in the UI). With the
fake forge, `POST /webhooks/fake` is accepted as well as `/webhooks/github`.

## Webhook handling

`POST /webhooks/github` checks, in this order, before parsing or writing anything:

1. A webhook secret is configured (`COVERSET_GITHUB_WEBHOOK_SECRET`, or the stored App's), else
   **503** (fail closed). The fixtures' secret `coverset-test-secret` counts as unconfigured
   unless `COVERSET_ENV=test`.
2. Bodies over 25 MiB are rejected with **413**.
3. `X-Hub-Signature-256` is an HMAC-SHA256 of the raw body under the secret (constant-time
   comparison; the legacy SHA-1 header is ignored), else **403** with no part of the body echoed.
4. `X-GitHub-Delivery` must look like a GUID (**400**); a delivery already recorded answers
   **200** `{"status": "duplicate"}` and does nothing. "Already recorded" means the same GUID *or*
   the same SHA-256 of the raw body: the signature covers only the body, so anyone who saw a signed
   delivery could otherwise resend it under a fresh GUID. (Two genuinely different deliveries are
   never byte-identical in practice; a double click on a check button may be, and is then
   handled once.)
5. `X-GitHub-Event` outside the adapter's allowlist (`pull_request`, `push`, `check_suite`,
   `check_run`, `installation`, `installation_repositories`, `repository`, `ping`; each forge
   adapter carries its own `allowed_events`) is recorded and answered **202** `ignored_event`.
6. The payload is parsed and handed to intake. Every SHA must match `^[0-9a-f]{40}$`; every numeric
   id (`repository.id`, `repository.owner.id`, `installation.id`, `pull_request.number`, ...) must
   be an integer in `0..2**63-1` (a PR number at least 1), so nothing overflows a database column;
   `repository.default_branch`, `pull_request.head.ref` and `base.ref` must be at most 255
   characters, pass `git check-ref-format --allow-onelevel`'s rules and not start with `-`;
   repositories are identified by `repository.id`, and `clone_url`/`ssh_url` are never read.
   A malformed payload is **400**, recorded as `error`, so GitHub's redelivery of it is a duplicate.
   The answer is **202** with `{"status": ..., "run_id": ...}`; forge calls (the approval check,
   the approver's permission check) and the run-queue hand-off happen after the database commit,
   so GitHub's 10 s budget is never at risk.

Steps 4-6 run one delivery at a time per process, so a burst
such as `opened` + `ready_for_review` for a repository coverset has not seen yet cannot race on its
unique keys. Should a unique-key conflict still happen, the whole intake is retried once; a second
failure answers **503** with nothing stored, so the delivery can be redelivered.

| Event | Effect |
|---|---|
| `pull_request` opened, synchronize, reopened, ready_for_review | PR upserted; a `pull_request` run for the head (drafts skipped unless `COVERSET_RUN_DRAFTS=true`) |
| `push` to the default branch | a trusted `baseline` run (pushes elsewhere are ignored) |
| `check_run` / `check_suite` rerequested | a `rerun` run copying the commit's last run |
| `check_run` requested_action `approve-run` | the sender's write access is checked, then the awaiting run is queued with the network blocked |
| `check_run` requested_action `rerun-failed` / `rerun-all` | a `rerun` run (scope recorded) |
| `installation`, `installation_repositories` | repositories recorded, or their installation cleared |
| `repository` privatized, publicized | the stored repository's privacy updated (never creates one) |
| `check_suite` requested, `ping`, anything else | stored and ignored |

Runs are deduplicated on `(repository, head SHA, trigger)`; with `COVERSET_PAUSED=true` new runs
are stored as `paused` and nothing starts.

### Fork and untrusted pull requests

A pull request is trusted when it does not come from a fork and its `author_association` is
`OWNER`, `MEMBER` or `COLLABORATOR`. Everything else follows `COVERSET_UNTRUSTED_PR_POLICY`:

* `approve` (default in App mode): the run waits as `awaiting_approval` and the PR gets a neutral
  `coverset: awaiting approval` check with an **Approve run** button. A press is only a request:
  coverset records who pressed it (`runs.approval_requested_by`), then asks GitHub for that
  person's role (`GET /repos/{owner}/{repo}/collaborators/{login}/permission`) and queues the run
  only for `admin`, `maintain` or `write`. The untrusted PR's own author is refused even with write
  access. A refusal is stored as an `approval_rejected` run event; if GitHub cannot answer, the run
  keeps waiting and the button can be pressed again. A new push to the PR cancels the waiting run
  and asks again.
* `run_blocked_network`: the run is queued at once.
* `skip`: no run.

Untrusted runs always run with the sandbox network blocked, never on the LocalExecutor, never
refresh snapshots or test statistics, and skip AI review unless `COVERSET_AI_REVIEW_UNTRUSTED=true`.
A PR's own code can falsify its own results: treat untrusted verdicts as advisory.

### Redelivery and replay

GitHub keeps every delivery under the App's (or webhook's) *Advanced → Recent deliveries*, and
*Redeliver* resends it with the same `X-GitHub-Delivery` and a byte-identical body. A delivery
coverset already recorded is answered 200 and ignored, so redelivering is only useful for
deliveries that failed before they were recorded (coverset down, 503 for a missing secret or a
write conflict, 403 for a rotated secret).

Locally, `coverset replay FIXTURE [--server http://127.0.0.1:8000]` posts a recorded fixture with
its exact body and headers. When `COVERSET_GITHUB_WEBHOOK_SECRET` is set to something other than
the fixtures' test secret, the body is re-signed with it (`--as-recorded` disables that); the
secret of an App stored by `/setup` is not read, so replaying against such a server needs that
variable set by hand.
`--new-delivery` sends a fresh delivery id, but the body digest still marks the same fixture as a
duplicate on a database that has seen it; start from a fresh `COVERSET_DATA_DIR` to replay it
again. The fixtures are generated from the seeded demo SHAs by
`uv run python scripts/make_fixtures.py`:

| Fixture | What it is |
|---|---|
| `pr_opened` | PR #1 "Support partial refunds", opened by a member |
| `pr_synchronize` | PR #2 "Allow signups from company subdomains", new head |
| `pr_opened_fork` | PR #3 from a fork, `author_association: NONE` |
| `push_main` | push of the last seeded commit to `main` |
| `check_run_rerequested` | "Re-run" on PR #1's check |
| `check_run_requested_action` | "Approve run" on PR #3's awaiting-approval check (`external_id` = run `run_fixture_fork_pr3`) |
| `installation_created` | the App installed on `coverset-demo` with `ledger` selected |
| `ping` | the App webhook's ping |

## Local tunnels

GitHub must reach `/webhooks/github`. **Set `COVERSET_ADMIN_TOKEN` (at least 32 characters)
before you start any tunnel, then set `COVERSET_PUBLIC_URL` to the tunnel URL.** Without a token
coverset runs in open mode, where every request is the operator. Open mode answers only requests
whose `Host` is a loopback name or the bind host (DNS-rebinding defence). It never accepts the
`COVERSET_PUBLIC_URL` host, and coverset refuses to start with a non-loopback public URL and no
token.

* **smee.io**: `npx smee-client --url https://smee.io/<channel> --target http://127.0.0.1:8000/webhooks/github`.
  The client re-posts each delivery from your machine, so `Host` stays `127.0.0.1:8000` and the
  rest of coverset stays private. Use the smee URL as the App's webhook URL (edit it after the
  manifest flow, or set `COVERSET_PUBLIC_URL` to it, with the admin token, while running
  `/setup`). **A smee channel URL is a secret**: anyone holding it can read every delivery's
  signed body as it streams. coverset refuses a body it has already seen even under a new GUID,
  but a captured delivery that never reached coverset could still be posted first by someone else.
* **ngrok** (or any reverse proxy): forwards the public `Host` (or, with
  `--host-header=rewrite`, `localhost` plus `X-Forwarded-*` headers). Either way the UI is
  reachable from the internet, which is why the admin token is required. Webhooks, `/v1/verdict`
  and the `/v1/runs/<id>` endpoints stay usable without the token; they authorize callers
  themselves. The `/setup` callback through a tunnel needs an admin session.
* **Rate limits behind a tunnel**: anonymous and OIDC rate limits are keyed on the client address,
  which behind a tunnel or proxy is the proxy's (usually 127.0.0.1). All anonymous callers, fork-PR
  CI jobs and strangers alike, then share one budget. coverset does not trust `X-Forwarded-For`
  (anyone can send it); raise `COVERSET_PUBLIC_VERDICT_RPM` if fork jobs hit 429s.

## The thin Actions step: `coverset-ci wait`

coverset starts testing when the webhook arrives, usually before an Actions runner has booted. The
CI job therefore does not run pytest; it waits for coverset's verdict and replays the results in
pytest's format, so the job's log, exit code and JUnit file look like a normal test job:

```yaml
name: tests
on: [pull_request, push]
permissions:
  contents: read
  id-token: write   # lets the step prove which repository it runs for
jobs:
  coverset:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-python@v5
        with:
          python-version: "3.13"
      - uses: <owner>/<repo>/coverset/src/coverset/forge/gha@<sha>
        with:
          server: https://coverset.example.com
          package: git+https://github.com/<owner>/<repo>@<sha>#subdirectory=coverset/packages/pytest-coverset
          junit: coverset-junit.xml
```

The composite action (`src/coverset/forge/gha/action.yml`) installs `coverset-ci` from the
required `package` input. There is deliberately no default: `pytest-coverset` is not published on
PyPI, and a bare name would install whatever someone registers under it, inside a job that can mint
OIDC tokens. Pin a VCS URL to a commit SHA as above (or, once the plugin is published, an exact
version with `--require-hashes`). The install runs with `ACTIONS_ID_TOKEN_REQUEST_URL` and
`ACTIONS_ID_TOKEN_REQUEST_TOKEN` removed from its environment, so a build backend or dependency
cannot request tokens. The next step requests an OIDC token only when
`ACTIONS_ID_TOKEN_REQUEST_URL` is set, masks it with `::add-mask::`, keeps it in the environment of
that one step, and runs

```sh
coverset-ci wait --server "$SERVER" --repo-id "${{ github.repository_id }}" \
  --sha "${{ github.event.pull_request.head.sha || github.sha }}" --token-env COVERSET_OIDC_TOKEN
```

`--repo-id` is GitHub's numeric repository id. The exit code follows pytest: 0 passed, 1 failed,
2 interrupted, errored, cancelled or timed out, 5 no tests, 3 when the server could not be reached.
The `exit_code` in the run's `done` event wins over guesses from the counts (an errored run with no
tests exits 2, not 5), but never turns anything other than `success` into 0. A `failure` verdict, the early one included, ends the wait at once with 1; after any other verdict the wait reads on to the `done` event, whose `exit_code` decides (a run that collected nothing sends `neutral` and then `done` with 5, so it exits 5).

The server checks the token with GitHub's JWKS
(`https://token.actions.githubusercontent.com/.well-known/jwks`, keys cached for an hour; an unknown
`kid` or a failed JWKS fetch is answered from a 30 s negative cache, so random-`kid` tokens cannot
stall verification for everyone while GitHub's endpoint is slow): RS256
only (`none` and HMAC tokens are refused before any key lookup), issuer
`https://token.actions.githubusercontent.com`, audience `COVERSET_OIDC_AUDIENCE` (default
`COVERSET_PUBLIC_URL`; a github.com audience is refused at startup), 60 s leeway, and the claims
`exp, iat, nbf, iss, aud, sub, repository_id, repository_owner_id` must be present. The token's
`repository_id` and `repository_owner_id` must match the repository coverset stored from a verified
webhook. Bearer requests are limited to 1200 a minute per client address before the token is
verified. Fork PRs get no OIDC token; without one, coverset answers only for repositories GitHub
reported as public (`private: false`) and limits each client address to
`COVERSET_PUBLIC_VERDICT_RPM` verdict and run lookups a minute (0 turns the anonymous path off).
Unknown and private repositories are both a plain 404. Anonymous callers get a reduced view:

* `/v1/verdict` returns only state, conclusion, run id and report URL;
* `/v1/runs/<id>` leaves the free-text reason out of `summary`;
* events are limited to the kinds `coverset-ci` needs (`progress`, `test`, `verdict`, `done`) with
  their documented fields only: no `log` events, `longrepr` always `null`, and no delivery ids,
  requesters or approvers;
* JSON polls have their own budget (twice `COVERSET_PUBLIC_VERDICT_RPM`), at most 4 anonymous
  event streams per client address and 64 in all may be open, and each ends after 120 s (the client
  reconnects with `Last-Event-ID`).

### API used by the step

| Endpoint | Answer |
|---|---|
| `GET /v1/verdict?repo_id=<id>&sha=<40hex>` | `{state, conclusion, run_id, summary, summary_url}` for the newest run of that commit; 404 until one exists. The first answer records `runs.ci_first_contact_at`. |
| `GET /v1/runs/<run_id>` | `{run_id, state, conclusion, counts, summary, summary_url}` |
| `GET /v1/runs/<run_id>/events?since=<seq>` | `{events: [{seq, kind, payload}], done}` (`since` and `Last-Event-ID` are integers of at most 18 digits, `repo_id` at most 19; anything else is a 400) |
| same path with `Accept: text/event-stream` | SSE frames `id`/`event`/`data`, resumable with `Last-Event-ID`, a `: ping` comment at least every 15 s, ending after the `done` event |

## Upload mode

The tests keep running in the repository's own CI. `pytest --coverset` writes the bundle, and
`coverset-ci upload` (or the composite action `src/coverset/forge/gha/upload/action.yml`) sends it
to `POST /v1/bundles`. coverset then computes patch coverage, input-class gaps and invariants, and
posts the same sticky comment and check run. The upload authenticates with the job's OIDC token
(`permissions: id-token: write`; a repository coverset does not know yet is registered when its
owner is in `COVERSET_ALLOWED_OWNERS`), or with a repository token that has the `upload` scope.
Patch coverage needs a way to fetch the repository: the App installed, or token mode. Without one,
the run still gets totals, gaps and invariants, and its reason says
`no diff available: install the GitHub App or set a base`. There is no anticipatory ordering, no
warm start, and results arrive after CI finishes. See [docs/upload.md](upload.md), and
[docs/examples/coverset-upload.yml](examples/coverset-upload.yml) for a workflow.

## Future work: merge queues

`merge_group` events (GitHub merge queues) are not handled: the App does not subscribe to them and
the route ignores them. Supporting them means running the queue's temporary merge commit as its
own run, reporting the `coverset` check on that commit, and treating the queue entry as trusted
only when every PR in it is.
