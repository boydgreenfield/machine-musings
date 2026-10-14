# Upload mode

Upload mode is the fastest way to put a repository on coverset. The tests keep running where they
run today, in the repository's own CI (GitHub Actions or anything else). The job runs
`pytest --coverset`, which writes a bundle to `.coverset/`, and `coverset-ci upload` sends that
bundle to coverset. coverset ingests it, computes patch coverage, input-class gaps and invariant
statuses for the commit, and posts the sticky PR comment and check run. There is nothing to set up
for Modal, runners or environment export, and no webhook is needed to begin.

```yaml
permissions:
  contents: read
  id-token: write
steps:
  - uses: actions/checkout@v4
    with:
      ref: ${{ github.event.pull_request.head.sha || github.sha }}
  # ... set up Python and install the project's test dependencies as usual ...
  - run: python -m pip install "git+https://github.com/<owner>/<repo>@<sha>#subdirectory=coverset/packages/pytest-coverset"
  - run: python -m pytest --coverset
  - if: always()
    run: coverset-ci upload --server https://coverset.example.com --with-sources
```

Those are the three lines. The composite action
[`src/coverset/forge/gha/upload/action.yml`](../src/coverset/forge/gha/upload/action.yml) does the
same thing in one step, and keeps tokens away from the code under test (see [Security](#security)).
A complete workflow with a two-job matrix is in
[`docs/examples/coverset-upload.yml`](examples/coverset-upload.yml).

## How it works

```
CI job                                   coverset
------                                   --------
pytest --coverset  ──► .coverset/
coverset-ci upload ──POST /v1/bundles──► authorize (token | OIDC | admin) before reading the body
   (gzipped tar,       X-Coverset-*      stream to <runs_dir>/.uploads (0600), cap, sha256
    chunked)           headers           unpack under the untrusted-bundle limits
                                         register: run per (repo, commit), shard per label  ── 202
                                         background pass over every shard of the run:
                                           mirror (or uploaded sources) → head index, diff
                                           ingest each shard → finalize facts
                                           results, history, report, verdict, comment, check
coverset-ci upload --wait ◄── GET /v1/bundles/<run_id> ──  state, conclusion, exit code
```

1. **The client** (`packages/pytest-coverset/src/pytest_coverset/ci_upload.py`, standard library
   only) packs `bundle.tar.gz`, the neutral bundle (`neutral.tar.gz` or `neutral/`), `meta.json`
   and, with `--with-sources`, the measured source files under `sources/` into one gzipped tar.
   The archive is reproducible (gzip mtime 0, ownerless and timeless members), so the same files
   always give the same bytes and the same sha256. It streams the archive with
   `Transfer-Encoding: chunked`. If a proxy answers 411, the client sends it again with a
   `Content-Length`. The run is named in headers, so the server can authorize before reading the
   body:

   | Header | Value |
   |---|---|
   | `Authorization` | `Bearer <cst_ token or GitHub Actions OIDC token>` |
   | `X-Coverset-Repo` | `OWNER/NAME` |
   | `X-Coverset-Sha` | the 40-hex commit the tests ran on (for a PR, its head) |
   | `X-Coverset-Base` | optional 40-hex base commit (the PR's base, or a push's `before`) |
   | `X-Coverset-Branch`, `X-Coverset-Pr-Title` | optional, percent-encoded |
   | `X-Coverset-Pr` | optional PR number |
   | `X-Coverset-Label` | the shard's name (default `COVERSET_LABEL`, else `GITHUB_JOB`, else `default`) |
   | `X-Coverset-Shards` | optional: how many labels this commit uploads |
   | `X-Coverset-Archive-Sha256` | the archive's sha256 (checked by the server) |

   In GitHub Actions the defaults come from the environment. `--repo` is `GITHUB_REPOSITORY`. For
   `pull_request` events the client reads the head SHA, base SHA, number and title from the event
   payload (`GITHUB_EVENT_PATH`). It never uses `GITHUB_SHA`, because that is GitHub's temporary
   merge commit. For `push` events the client uses the pushed commit, with the `before` SHA as the
   base. The branch comes from `GITHUB_HEAD_REF` or `GITHUB_REF_NAME`. When the checkout is not the
   commit being uploaded, the client prints a warning: `actions/checkout` checks out the merge
   commit by default, and then line numbers drift wherever `main` changed the same files.

2. **`POST /v1/bundles`** (`src/coverset/web/uploads.py`) authorizes the caller, enforces the size
   cap while it streams the body to disk, checks the digest, and unpacks the archive under the
   strict untrusted-bundle limits (`facts/untrusted.py`: no links, no devices, no `..`, member and
   size caps; each inner bundle must fit `COVERSET_MAX_BUNDLE_BYTES`). It then registers the upload
   (`orchestrator/upload.py`, `register_upload`) under a per-commit lock and answers
   `202 {run_id, url, pr_url?, state, shard, label, duplicate}`.

3. **The processing pass** (`UploadWorker`, `process_run`) runs in the background, once per run at
   a time. An upload that lands during a pass triggers exactly one more pass. Each pass recomputes
   everything from every shard of the run, so the order shards arrive in and the number of replays
   make no difference:
   * **head:** coverset refreshes the repository's mirror the same way as for managed runs (App
     installation token, token-mode credentials, or a recorded local source) and exports the head
     commit. If the mirror has no copy of the commit, it uses the uploaded sources instead. If there
     are none, there is no checkout;
   * **diff:** `merge-base(base, head)...head` on the mirror, classified into changed functions
     with the planner's own helpers;
   * **ingest:** each shard goes through `bundles.ingest_one`. With a head checkout, coverset
     ingests the pytest bundle with its own function index of the head sources, exactly as in
     managed mode. Without one, it ingests the neutral bundle with the regions and declared types
     the bundle ships (`IngestJob.regions_from_bundle`). A Vitest upload (only a neutral bundle)
     takes the neutral path in both cases;
   * **verdict:** `finalize_run` expects one shard per label (or `--shards`). The pass then writes
     results and test history, runs `build_report`, `build_summary` and `persist_verdict`, and adds
     `verdict` and `done` events. Last, the forge posts the summary through `post_summary` (check run
     or commit status, and the sticky comment). These are the same functions a managed run uses.

4. **`GET /v1/bundles/<run_id>`** answers the same credentials with
   `{state, conclusion, exit_code, counts, summary, patch_pct, failures, shards}`.
   `coverset-ci upload --wait` polls it, starting at 0.25 s and backing off to 3 s, prints the
   verdict, patch coverage and failures, and exits with pytest's codes.

## What is computed

| | With the GitHub App (or another way to fetch the repository) | Without a mirror, with `--with-sources` | Without either |
|---|---|---|---|
| Test results, test history, the failure list | yes | yes | yes |
| Line and branch totals, per-test coverage, source view | yes | yes (source view from the upload) | totals only; the source view has no text |
| Patch coverage, changed-function table, inherited changes | yes, when a base is known | no | no |
| Input classes of changed functions | yes | no | no |
| Largest input-class gaps of the run | on the pages | named in the verdict's reason | named in the verdict's reason |
| Invariant statuses | yes | yes, when `invariants.yaml` came with the sources | no |
| Function regions | coverset's index of the head sources | coverset's index of the uploaded sources | the regions the neutral bundle ships |
| Reason suffix | none | `no diff available: install the GitHub App or set a base` | the same |

"A mirror" means coverset can fetch the repository: the App is installed (installation token),
`COVERSET_GITHUB_TOKEN` can read it, or the repository's row records a local source (the demo).
Without a base commit (a push with no `before`, or an upload without `--base` outside Actions)
there is no diff either. The reason then says so, and it names the run's largest input-class gaps
instead of the changed functions' gaps.

## Server settings

| Setting | Default | Meaning |
|---|---|---|
| `COVERSET_UPLOADS_ENABLED` | `true` | `false` answers 404 on both `/v1/bundles` routes |
| `COVERSET_MAX_UPLOAD_BYTES` | 200 MiB | largest upload archive, checked against `Content-Length` and again while streaming (413) |
| `COVERSET_MAX_BUNDLE_BYTES` | 200 MiB | largest inner `bundle.tar.gz` / `neutral.tar.gz` (413) |
| `COVERSET_ALLOWED_OWNERS` | none | owners whose repositories an OIDC upload may register (see below) |
| `COVERSET_ALLOWED_REPOS` | none | further narrows auto-registration to these `owner/name`s |
| `COVERSET_OIDC_AUDIENCE` | `COVERSET_PUBLIC_URL` | the audience OIDC tokens must carry; the client requests the server URL by default (`--audience` or `COVERSET_AUDIENCE` to change it) |

The app's global body limit (`MAX_CONTENT_LENGTH`, 25 MiB) applies to requests that declare a
`Content-Length`, and it rejects them before any view runs. The client therefore sends chunked
bodies, and the upload view enforces `COVERSET_MAX_UPLOAD_BYTES` itself. A declared length above
25 MiB, such as the 411 fallback behind a strict proxy, is refused with 413 until `/v1/bundles`
is exempted from that global limit.

When either cap trips while the body is still streaming, the server answers and closes the
connection before the client has finished sending, so the client usually sees a dropped connection
(`BrokenPipeError`, `ConnectionResetError`) rather than the 413 itself. `coverset-ci upload` tries
such a drop once more and then stops with exit 3 and the message "the server closed the connection
while receiving the archive (N bytes): it is probably over the server's size cap
(COVERSET_MAX_UPLOAD_BYTES)", instead of retrying it as a network outage.

## Permissions and credentials

One of these is required. Any other request gets 401.

* **GitHub Actions OIDC** (recommended in Actions, no secret to manage): give the job
  `permissions: id-token: write`. The client requests a token itself when
  `ACTIONS_ID_TOKEN_REQUEST_URL` and `ACTIONS_ID_TOKEN_REQUEST_TOKEN` are set. The server verifies
  it (RS256 with GitHub's JWKS, issuer, audience, expiry). The token's `repository` must be the
  repository named in `X-Coverset-Repo`, or the server answers 403. Its `repository_id` and
  `repository_owner_id` must match the stored repository, or the server answers 403. If coverset
  does not know the repository yet, it **creates the repository row** from the token's claims, but
  only when the owner is listed in `COVERSET_ALLOWED_OWNERS` (and passes `COVERSET_ALLOWED_REPOS`).
  The row is private unless the token says `repository_visibility: public`. Any other owner gets
  404. This is how a private repository joins coverset without installing the App.
* **A repository token** with the `upload` scope, passed as `--token` or `COVERSET_TOKEN` (store it
  as an Actions secret): `coverset users tokens create --repo OWNER/NAME --scopes upload --name ci`,
  or the token settings page. It uploads for its own repository only, and any other repository gets
  403. The repository row must exist already: the App or a webhook creates it, or one OIDC upload
  does. A **personal token** with the `upload` scope works for repositories its user may push to
  (`repo_access` write or admin), and uploads to a repository with read access get 403.
* **The admin token** (`Authorization: Bearer $COVERSET_ADMIN_TOKEN`) uploads for any repository
  coverset knows.

A token without the `upload` scope gets 403. Requests with a non-admin bearer are limited to 600
a minute per client address, counted before the view verifies the credential. Accepted uploads are audited as
`bundle_uploaded`, with the uploader, label, commit, size and sha256.

## Security

* **Who can upload is who can run CI with credentials.** GitHub gives fork pull requests no OIDC
  token and no secrets, so a fork's CI cannot upload. A same-repository PR is opened by someone with
  push access. Runs from `pull_request_target` OIDC tokens are recorded **untrusted**: that event
  runs base-branch workflows with secrets, and if the workflow checks out the PR head, the PR's code
  runs with them. An untrusted run does not update test history, and the AI review skips it unless
  `COVERSET_AI_REVIEW_UNTRUSTED` is set.
* **Results are self-reported.** An upload proves which repository sent it, not that its tests are
  honest. The PR's own code runs in that job and could write any bundle it likes. The same holds for
  managed runs of untrusted PRs. Every bundle is parsed as untrusted data either way: caps, no links,
  checked paths, schema validation.
* **Keep tokens away from the tests.** The composite action installs the plugin and runs pytest
  with `ACTIONS_ID_TOKEN_REQUEST_URL`, `ACTIONS_ID_TOKEN_REQUEST_TOKEN` and `COVERSET_TOKEN` removed
  from their environment. Only the upload step can request an OIDC token or see the repository
  token. The three-line form does not do this: there the test step could mint OIDC tokens for the
  job. Use the action, or a repository token kept out of the test step's `env`, when that matters.
* **Sources never include secrets by accident.** `--with-sources` sends only paths that pass two
  checks: the neutral bundle's `files` table lists them, plus `pyproject.toml` and the configured
  invariants file, and `git ls-files` reports them as tracked. Symlinks, missing files and files over
  2 MiB are skipped, and the client prints the count of each. The server applies the same rule
  again: it stores only paths the shard's own bundle lists, plus those two config files, under
  `<data_dir>/sources/<repo_id>/<sha>/` (directories 0700, files 0600, created with
  `O_EXCL|O_NOFOLLOW`). A commit's files are written once, and a later upload cannot change them. A
  member path with `..`, an absolute path or a link makes the whole archive fail with 400. The
  source view reads the store only for paths the run's facts list, and only after the mirror had no
  copy.
* **No network for untrusted input.** The server never follows a URL from the upload. It fetches a
  mirror only from the forge's canonical URL, or from a source recorded by an admin.

## Replay and idempotence

The key is `(repository, commit, label, archive sha256)`. The server keeps the label and the
first 32 hex characters of the sha256 in `shards.worker_ref` (`upload:<sha256>:<label>`).

* Same archive, same label: `202 {"duplicate": true}`. The response names the existing run and
  shard, and nothing changes. A re-run of the upload step, or the client's own retries after a
  network error, are free. If the run is still being processed, one more pass is scheduled.
* Different archive, same label: that shard is replaced (a re-run of the CI job), and the run is
  processed again.
* New label: a new shard (see below).
* A run the control plane's boot-time takeover `abandoned` (it was `running`, waiting for its
  matrix legs or mid-pass, when the server restarted) is re-opened by the next upload for the
  commit: its shards are still on disk, so the late legs conclude the same run. With
  `COVERSET_REQUEUE_ON_BOOT=true` the takeover also queues a copy for the planner; the next upload
  cancels that copy while it is still queued (an upload run is never executed).
* A run that ended in `error` or `cancelled` is left alone, and the next upload for the commit
  starts a new run.

## How shards merge

Every label of a commit is one shard of one run (`trigger = upload`). Shard ids are given out in
arrival order. Give every matrix leg its own `--label` (the action's `label` input): the default,
`GITHUB_JOB`, is the job id, which is the same for every leg of a matrix job, so without a label
the legs would replace each other's shard. The CLI refuses `--shards N` without an explicit label
(`--label` or `COVERSET_LABEL`), and the action fails before running the tests when `shards` is
set and `label` is not. Without `--shards`, every pass concludes: the verdict, `verdict` and `done` events,
and the forge post. A later shard re-opens the run (`running`), and the run is concluded again with
every shard included. The sticky comment and the check run are updated in place, never duplicated.
`coverset-ci wait` and the event stream end at the first `done`, so with several shards and
without `--shards` they report the first shard's view. With `--shards N` the run stays `running`,
and the verdict and the forge post wait until N labels are in. Until then, a `log` event says
`k of N uploads in`, and the facts stay finalized with `coverage_complete = false`, so the pages
show the shards that have arrived. If two shards report the same test, the later shard's result
wins, and its coverage is kept once (the facts store's rule for duplicate tests).

## Compared with managed runs

| | Upload mode | Managed runs (App + executor) |
|---|---|---|
| Where tests run | the repository's CI | coverset's workers (Modal sandboxes or local) |
| Setup | three lines, OIDC | App, executor, `[tool.coverset]`, environment export |
| When results arrive | after CI finishes | while the PR's tests are still running; the first failure early |
| Test ordering by risk, wave 1, sharding | no: the tests already ran in CI's order | yes |
| Warm pools, trusted snapshots | no | yes (Modal) |
| Retries and flaky detection | no: a failure is final | one retry, flaky marked |
| Pre-existing failures on the base | not separated | separated |
| Patch coverage, input classes, invariants, comment, check | yes (patch coverage needs a mirror and a base) | yes |
| Cost to coverset | ingest only | sandboxes |

The two modes combine. Upload mode can start today; once the App is installed, the same repository
gets patch coverage from its uploads, and later managed runs if wanted. A repository that relies
on uploads alone should not also get webhook-triggered managed runs: leave
`COVERSET_LOCAL_ALLOW_WEBHOOK_RUNS` off, or do not install the App on it.

## Numbers

Measured on 2026-10-13 on an Apple-silicon laptop, with the demo repository's PR #2 (99 tests) and
a local Hypercorn server (`tests/e2e/test_upload_e2e.py` setup, 3 runs):

* `pytest --coverset` in the checkout: 3.2 s;
* upload archive with sources: 68.7 KB (bundle, neutral bundle, `meta.json` and 22 tracked
  source files);
* `coverset-ci upload --wait`, from the start of the command to the printed verdict (mirror
  refresh, diff, ingest, report, comment): 0.95 s, 0.96 s and 2.1 s; a replay of the same archive:
  0.14 to 0.22 s; a second label (two-shard pass): 0.94 to 2.1 s.

## Caveats

* Each pass re-reads every shard's bundle (ingest of an unchanged shard is a no-op in the facts
  store, but conversion still runs), so a run of k shards costs O(k²) conversions in all. That is
  fine for a matrix of a few jobs. Hundreds of shards per commit would need per-shard outcome
  caching.
* Processing runs in the serving process. An upload that is being processed when the server
  restarts leaves its run `running` until the boot-time takeover abandons it. The next upload of
  any shard re-opens the run (see "Replay and idempotence"); until then the run reads `abandoned`
  and `coverset-ci upload --wait` reports it as neutral with exit 2.
* The check run that upload mode posts offers the usual buttons. "Re-run all" queues a managed run,
  which needs an executor; in an upload-only setup, re-run the CI job instead.
* The uploaded-source store is not covered by retention (`coverset gc`) yet.
* The OIDC token's `sha` claim is not bound to `X-Coverset-Sha`: for pull requests it names the
  merge commit, not the head. A job can therefore upload for any commit of its own repository.
