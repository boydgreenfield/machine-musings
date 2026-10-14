# Settings: instance, organization, repository and user scopes

coverset has two kinds of configuration. `COVERSET_*` environment variables (`src/coverset/config.py`) describe the deployment: credentials, the executor, budgets, hard limits. **Scoped settings** describe behaviour that differs between teams and repositories, and that people with the right role change from the web UI or the CLI without a restart. This page is about the second kind. The code is `src/coverset/settings_scopes.py` (catalogue, validation, resolution, cache), `src/coverset/web/settings_pages.py` (pages, token pages, the `view_prefs` template context) and `src/coverset/commands/settings_cmd.py` (CLI).

## The model

A setting is one `scoped_settings` row: `(scope_kind, scope_id, key) -> {"v": value}` (migration 0008). Four scopes:

| Scope | `scope_id` | Covers |
|---|---|---|
| `instance` | `""` | every repository on this control plane |
| `org` | the owner, lower-cased (`acme`) | every repository whose name starts with `acme/`; GitHub owner names are case-insensitive. Local repositories registered by `coverset run` are under `local/` |
| `repo` | `repos.id` | one repository; keyed by id, never by name, so a rename keeps its settings |
| `user` | `users.id`, or `local` | one person's view preferences. `local` is the operator of an open-mode or admin-token instance, who has no `users` row |

Keys belong to two groups that inherit differently:

- **View preferences** (`view.*`) change how pages paint coverage, so the reader decides last: **user > repo > org > instance > built-in default**.
- **Policy** (`policy.*`) changes what coverset does with a pull request, so the people who answer for the repository's CI decide: **repo > org > instance > `COVERSET_*` configuration > built-in default**. A user cannot set a policy key at all; `set_value` refuses it.

Every value is validated on write and again on read. A stored row that the catalogue no longer accepts (an enum value that was removed, a hand-edited row) is logged and skipped, so resolution falls through to the next scope instead of breaking a page or a run.

## Keys

The table is generated from the catalogue (`coverset settings keys --markdown`); `tests/test_settings_scopes.py` fails when the two drift apart.

<!-- settings-keys:start -->
| Key | Values | Default | Scopes | Who may edit | Meaning |
|---|---|---|---|---|---|
| `view.highlight` | `all` \| `missing` | `all` | instance, org, repo, user | user: the user; repo: repository write; org, instance: instance admin | Paint every measured line, or only the lines that never ran or took one branch. |
| `view.map_mode` | `gap` \| `lines` \| `inputs` | `gap` | instance, org, repo, user | user: the user; repo: repository write; org, instance: instance admin | What the coverage map colours by default: the input-class gap, line coverage, or input-class coverage. |
| `view.map_module_weight` | `full` \| `thin` \| `hidden` | `thin` | instance, org, repo, user | user: the user; repo: repository write; org, instance: instance admin | How much room module-level code (imports, constants) takes in the coverage map: full size, a thin strip, or hidden. |
| `view.theme` | `auto` \| `light` \| `dark` | `auto` | instance, org, repo, user | user: the user; repo: repository write; org, instance: instance admin | Light, dark, or follow the operating system. |
| `policy.untrusted_pr_policy` | `approve` \| `run_blocked_network` \| `skip` | `COVERSET_UNTRUSTED_PR_POLICY` (default `approve`) | instance, org, repo | repo: repository admin; org, instance: instance admin | Pull requests from forks or non-members: wait for a writer to approve, run with the network blocked, or skip them. |
| `policy.run_drafts` | bool | `COVERSET_RUN_DRAFTS` (default `false`) | instance, org, repo | repo: repository write; org, instance: instance admin | Start runs for draft pull requests too. |
| `policy.ai_review` | bool | `false` | instance, org, repo | repo: repository admin; org, instance: instance admin | Ask the advisory AI reviewer about changed functions (needs COVERSET_AI and, for the live reviewer, ANTHROPIC_API_KEY). Sends changed code to Anthropic at the instance's expense. |
| `policy.fail_fast` | `finish_in_background` \| `cancel_remaining` | [tool.coverset] fail_fast (default finish_in_background) | instance, org, repo | repo: repository write; org, instance: instance admin | Keep running the remaining tests in the background, or cancel them. |
| `policy.max_shards` | int 1..128 | [tool.coverset] max_shards (default 16) | instance, org, repo | repo: repository admin; org, instance: instance admin | Upper bound on parallel workers for one run: caps [tool.coverset] max_shards (never raises it) and is always capped by COVERSET_MAX_SHARDS. |
| `policy.target_shard_seconds` | int 1..3600 | [tool.coverset] target_shard_seconds (default 20) | instance, org, repo | repo: repository admin; org, instance: instance admin | How long each shard should take; smaller values ask for more shards. |
| `policy.test_tax_threshold` | float 0.5..1000 | `8` | instance, org, repo | repo: repository write; org, instance: instance admin | Added test statements per newly covered production line above which the PR verdict flags test bloat (the stored test-economy flag, the PR card and the comment). |
| `policy.public_report` | bool | `false` | instance, org, repo | repo: repository admin; org, instance: instance admin | Let anyone read a public repository's pages without signing in (private repositories are never public). Not yet enforced: the access layer does not read it yet. |
<!-- settings-keys:end -->

Notes on particular keys:

- **`policy.untrusted_pr_policy`** falls back to `COVERSET_UNTRUSTED_PR_POLICY`, including its token-mode default (`run_blocked_network` when coverset uses a personal access token, because token mode has no Approve button; `forge/registry.py`, `effective_untrusted_pr_policy`). Whatever this key says, the dispatcher still never runs an untrusted PR on the LocalExecutor and always blocks its network.
- **`policy.fail_fast`, `policy.max_shards`, `policy.target_shard_seconds`** have no instance configuration. When no scope sets them, the repository's own `[tool.coverset]` decides (defaults `finish_in_background`, 16 and 20 s). When a scope sets `fail_fast` or `target_shard_seconds`, it replaces the `[tool.coverset]` value, because that file is read from the pull request's head, which the PR author controls, while scoped policy is set by the repository's writers and admins. `max_shards` is different: it is an upper bound, so a run gets the smallest of the scoped value, the file's `max_shards` and `COVERSET_MAX_SHARDS`. Only raising the shard count costs the instance anything, so a repository whose file asks for 2 shards keeps 2 under an instance-wide cap of 8.
- **`policy.ai_review`** sends the repository's changed functions to Anthropic at the instance's expense, so at repo scope it needs repository **admin**, like the other keys that change cost or exposure. It also reads the older per-repository `repos.ai_review_enabled` column. The column counts as an implicit repository-level `true`: an explicit repository row wins over it, and it wins over organization and instance values, since it was always a per-repository opt-in. The live reviewer still needs `COVERSET_AI=anthropic` and `ANTHROPIC_API_KEY`; untrusted runs still need `COVERSET_AI_REVIEW_UNTRUSTED`.
- **`policy.test_tax_threshold`** is the number of added test statements per newly covered production line above which the verdict flags test bloat (the "95 lines of test scaffolding for one environment-variable check" case). It is enforced where the flag is computed and where it is read: `finalize_run` gets `EconomyInputs(policy=policy_from_scopes(effective_policy(...)))` from the runner and from the upload path (`orchestrator/runner.py`, `economy_inputs`), so the stored `run_economy` flag uses the repository's threshold, and the PR card, the sticky comment's tax line and the economy pages re-flag against the effective value when they read it (a threshold changed after a run applies to its pages at once).
- **`policy.public_report`** is meant to let anonymous callers read a public repository's pages, and never applies to a private repository (`repos.private`), whatever the value. **Not yet enforced**: the access layer does not read `effective_policy(...).public_report` yet, so saving it has no effect today.
- **`view.highlight`**: `missing` paints only lines that never ran or took one branch and leaves covered lines plain. **`view.map_module_weight`** controls how much room module-level code (imports, constants) gets in the coverage map, which otherwise over-weights it. The UI reads both through the `view_prefs` template variable.

## Who may change what

| Scope | Page | Who | Notes |
|---|---|---|---|
| instance | `/settings/instance` (`/settings` also works) | instance admins: the admin token, open mode, users with `role = admin` | `/settings/instance` is on the access guard's admin-only list as well. A non-admin who opens `/settings` is redirected to their preferences |
| org | `/settings/org/<owner>` | instance admins | the instance page's sub-navigation lists every owner that has a repository |
| repo | `/repos/<id>/settings` | write access to the repository | `policy.untrusted_pr_policy`, `policy.ai_review`, `policy.max_shards`, `policy.target_shard_seconds` and `policy.public_report` need repository **admin**, because they change cost or exposure. Writers see them read-only, labelled "needs repository admin". A read-only caller gets 403, and a caller who cannot see the repository gets 404 |
| user | `/account/settings` | the signed-in user, for themselves | view keys only; the operator without a user row stores under `user:local` |

A caller with an API token can read these pages with any scope, but needs `write` (or `admin`) to change a setting. Repository tokens and GitHub Actions OIDC callers have no preferences. The shell (`coverset settings ...`) is trusted like an instance admin.

Each page shows only the keys its scope accepts, grouped View and Policy. Each field shows what the scope would inherit if it set nothing ("inherited from organization acme: run with the network blocked") and carries a "Set here" or "Inherited" badge. Choosing **Inherit**, or pressing a row's **Reset to inherited**, deletes the scope's row. A POST validates every field before writing any of them, so one bad field changes nothing; the bad value stays in the field with an inline error (`aria-invalid`, 422 for a plain form, 200 for an htmx swap). Unchanged fields are skipped. The pages work without scripts as plain forms with a `csrf_token` field; with htmx, the form swaps in place.

Every change writes one `setting_changed` row to the audit log, with `{"op": "set"|"clear", "scope", "scope_id", "key", "old", "new"}`, through `coverset.web.audit.record_audit` (CLI changes use the `cli` actor).

## API tokens

`/settings/tokens` lists the caller's personal tokens and the tokens of every repository they may write (instance admins: all). The create form takes a name, a kind (`personal` acts as the user within its scopes; `repo` is bound to one repository and has no user), scopes (`read`, `write`, `upload`, `admin`; repository tokens accept only the first three) and a lifetime (7, 30, 90 or 365 days, or never). The raw `cst_...` token appears once, in a `Cache-Control: no-store` response. Only its sha256 and its 8-character prefix are stored, and the redactor masks `cst_` strings in logs and audit details. Revoking a token sets `revoked_at`; requests that use it are then refused. Owners may hold at most 50 live tokens.

- The admin token and open mode have no user row, so they can create only repository tokens (`user_id` NULL).
- A repository token created by a user records that user in `user_id`, so it is deleted along with the user.
- A token can never create or revoke tokens, whatever its scopes. Otherwise a leaked token could extend its own life.
- Creating and revoking are audited as `token_created` and `token_revoked`.

`coverset users tokens ...` does the same from the shell.

## How policy reaches the orchestrator

`effective_policy(session, repo, settings) -> Policy` resolves every `policy.*` key for one repository in one cached read and returns a frozen dataclass. Its `sources` field records where each value came from (`repo`, `org`, `instance`, `config`, `deferred`, `default`, `repo-column`).

| Reader | Uses | Where |
|---|---|---|
| intake | `untrusted_pr_policy` (approve, run blocked, skip, and whether a rerun of an unapproved run needs approval again), `run_drafts` | `orchestrator/intake.py`, `_Intake._policy` |
| planner | `Policy.apply(config, instance_max_shards=COVERSET_MAX_SHARDS)` right after `[tool.coverset]` is read, so `fail_fast` (read by the runner), `max_shards` and `target_shard_seconds` (shard estimate and dealing) all see the scoped values; `max_shards` is the smallest of the scoped cap, the file's value and `COVERSET_MAX_SHARDS` | `orchestrator/planner.py`, `plan_run` |
| verdict | `ai_review` decides whether the review client is built for the repository | `orchestrator/verdict.py`, `_ai_review` |
| test economy | `test_tax_threshold` (the stored flag at finalize, with the tests' `test_stats` p50s as durations; readers re-flag with the effective value) | `orchestrator/runner.py`, `economy_inputs` (managed and upload runs); the readers in `orchestrator/verdict.py` (`tax_line`) and `web/economy.py` |
| access layer | `public_report` | not yet wired: anonymous reads of public repositories ignore it |

## View preferences in templates

Every template gets `view_prefs`, a dict with the four `view.*` keys that can also be read by short name (`view_prefs.highlight`, `view_prefs['view.map_mode']`). It is resolved for the current reader (user scope, else `local` for the operator) and the page's repository (the `rid` URL argument). It is computed once per request and cached on `g`. If the database cannot be read, it falls back to the built-in defaults instead of failing the page. Python code calls `view_prefs(session, repo=..., user_id=...)`. Templates also get `settings_admin` and `settings_can_write(repo)`, which the nav partials `_nav_admin.html` and `_nav_repo_settings.html` use for their **Settings** links.

## Caching and processes

Reads go through a per-process cache keyed by `(database engine, scope_kind, scope_id)`. Each scope's rows are loaded once, with every uncached scope in a single query, and kept for 30 seconds (`CACHE_TTL_S`). A write in the same process invalidates its scope after the commit, so the page that saved a value shows it immediately. Another process (the CLI, a second `serve` worker) sees the change within the TTL. In steady state a page view runs no settings query; a cold one runs at most two (the repository's owner for the org scope, which is cached as well, then every uncached scope). The cache is weakly keyed by engine, so a re-created app or a test never sees another database's rows.

## Deleting a repository or a user

Repo and user scopes are keyed by `repos.id` and `users.id` without a foreign key, and SQLite gives a deleted row's id to the next insert. Whoever deletes a repository or a user calls `forget_scope(session, "repo" | "user", id)` in the same transaction (`coverset repo purge` does, in the transaction of its `Repo` delete). As a safety net, a repo-scope row last written before its repository's `created_at` counts as an orphan of a purged predecessor: reads ignore it (and log it), and setting the same value again at the new repository rewrites it. Without this, purging a fork and registering another repository could hand the newcomer the old one's `run_blocked_network` untrusted-PR policy or `public_report = true`.

## CLI

```
coverset settings keys [--markdown]                      # the catalogue
coverset settings list [--repo OWNER/NAME] [--user LOGIN]  # every key, effective value and source
coverset settings get KEY [--repo ...] [--user LOGIN] [--value-only]
coverset settings set KEY VALUE [--repo ... | --org OWNER | --user LOGIN]   # instance by default
coverset settings clear KEY [--repo ... | --org OWNER | --user LOGIN]
```

`--repo` accepts a full name, an id or a local source path. `--user local` is the operator. Values use the same parser as the pages: booleans are `true/false/1/0/yes/no/on/off`, numbers are plain ASCII.

## Prepared for later

- **More keys**: add a `SettingSpec` to `CATALOGUE`, regenerate the table above, and read the value with `resolve`, `effective_policy` or `view_prefs`. The pages, the CLI, validation and the audit trail need no other change.
- **Production data flow** (OpenTelemetry, Sentry, Datadog): anonymisation rules, sampling rates and retention are policy that will differ per organization. They fit as `policy.*` keys at org and repo scope, edited by repository admins.
