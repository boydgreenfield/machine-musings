# Sign-in, roles and tokens

Who may see what on a coverset that several people share. The short version: people sign in
with GitHub through the App coverset already has, and each sees exactly the repositories GitHub
lets them see, with the permission GitHub gives them there. The operator keeps the admin token.
Machines (CI jobs, an agent) use API tokens. Everything that changes state is written to an
audit log.

Code: `web/auth.py` (the access guard, sessions), `web/account.py` (sign-in, the account and
audit pages), `web/principal.py` (the contract every view uses), `web/tokens.py` (API tokens),
`web/audit.py`, `forge/oauth.py` (the GitHub calls), `commands/users.py` (the CLI). Tests:
`tests/web/test_account.py`, `tests/web/test_rbac.py`, `tests/test_tokens.py`, `tests/test_auth.py`.

## How sign-in works

The login page (`/login`) offers up to two options, depending on what is configured:

| Option | Offered when | What it does |
|---|---|---|
| **Sign in with GitHub** | the App has a client id and secret: the App `/setup` stored (offered within seconds of storing it, no restart), or `COVERSET_GITHUB_APP_CLIENT_ID` and `COVERSET_GITHUB_APP_CLIENT_SECRET` in the environment | the authorization-code flow of the GitHub App; creates a `users` row and a server-side session |
| **Admin token** | `COVERSET_ADMIN_TOKEN` is set | the form (or JSON `POST /login`) sets the admin flag in the cookie session; this is the operator |

With neither configured coverset is in open mode (loopback only, no token): every caller on the
machine is the operator and `/login` is a 404. A GitHub sign-in in open mode still works but
changes nothing: the caller was already the operator.

The GitHub flow, step by step:

1. `GET /auth/github?next=/repos/1` stores a random `state` and the validated `next` path in the
   cookie session and redirects to `https://github.com/login/oauth/authorize?client_id=...&redirect_uri=<COVERSET_PUBLIC_URL>/auth/github/callback&state=...`.
   **No `scope` is requested**: a GitHub App's user token carries the intersection of the App's
   permissions and the user's own, so there is nothing to ask for and nothing to over-grant.
2. GitHub sends the browser to `GET /auth/github/callback?code=...&state=...`. The `state` is
   compared in constant time and used once; a wrong one is a 400. `error=...` from GitHub
   (the person pressed Cancel) is a 400 with the slug.
3. coverset exchanges the code (`POST https://github.com/login/oauth/access_token`, `Accept:
   application/json`), fetches `GET /user`, and creates or refreshes the `users` row: forge
   `github`, `external_id` = the numeric GitHub id (the login is display only; logins can change
   hands), login, name, avatar. The user token (`ghu_`), its refresh token (`ghr_`) and the
   expiry are stored on the row. New GitHub Apps issue tokens that expire after 8 hours; coverset
   refreshes one that is within five minutes of expiry before using it, and stores the rotated
   refresh token.
4. A row in `user_sessions` is created and its raw id placed in the cookie session (the row holds
   the sha256, so a database read-out cannot be replayed as a cookie). The cookie session is reset
   first, so a session id planted before sign-in never survives it.
5. coverset asks GitHub which of its repositories the user may see (next section), records a
   `login` audit event and redirects to `next`.

The manifest `/setup` posts registers the callback: `callback_urls` is
`[<COVERSET_PUBLIC_URL>/auth/github/callback]` and `request_oauth_on_install` is `false` (installing
the App never starts a sign-in). An App created before this existed needs the callback URL added
by hand under the App's settings on GitHub ("Callback URL"), and its client secret generated there
and written to `.env` as `COVERSET_GITHUB_APP_CLIENT_SECRET`.

## Repository access

After sign-in, and then every 10 minutes while the person uses coverset (in the background, at
most once per 10 minutes per user per process), and whenever they press **Refresh from GitHub** on
`/account`, coverset recomputes the `repo_access` rows of that user:

* for every repository coverset watches that has an `installation_id`, one call to
  `GET /user/installations/{installation_id}/repositories` (100 a page) lists the repositories of
  that installation the user may see, with GitHub's `permissions` object. A repository absent from
  the listing is one the user cannot see. When the listing answers 403 or 404 (the user is not a
  member of that installation), the repositories fall back to the per-repository check;
* every repository without an installation id (and the fallbacks) is asked for with
  `GET /repos/{owner}/{name}`; GitHub answers 404 for a private repository the user cannot see.

GitHub's permission flags collapse to the three coverset distinguishes:

| GitHub `permissions` | coverset |
|---|---|
| `admin` | `admin` |
| `maintain` or `push` | `write` |
| `triage` or `pull` | `read` |
| none true, or 404 | no row: the repository does not exist for this user |

The table is then exactly GitHub's answer: rows are inserted, updated and deleted. The instance
role (next section) is not touched by this.

**Rate-limit cost.** A user token has its own budget of 5,000 requests per hour. A sign-in costs
one `/user` call plus the access refresh; a refresh costs one call per installation the user
belongs to plus one per repository that needed the fallback. With one installation and no
fallbacks that is one call every 10 minutes per active user, 6 an hour. The App's installation
tokens (used for checks and comments) are a separate budget.

## Identity and roles

Every request resolves to one **principal** (`web/principal.py`), and every repository-scoped view
starts with `require_repo(rid, "read" | "write")`:

| Principal | Who | Sees | Changes |
|---|---|---|---|
| **admin** | the admin token (bearer or its cookie), or open mode | everything | everything, including setup, the outbox, instance settings, the audit log |
| **user**, role `admin` | a GitHub user listed in `COVERSET_ADMINS` or promoted with the CLI | everything | everything |
| **user**, role `member` | any other GitHub user | the repositories in their `repo_access` rows | what they hold `write` or `admin` on: approvals of untrusted runs, quarantine, repository settings |
| **token**, personal | a `cst_` token tied to a user | the user's repositories, within the token's scopes | the user's writes, only with the `write` scope; the user's admin powers only with the `admin` scope (and the user must be an admin) |
| **token**, repository | a `cst_` token tied to one repository | that repository only | `upload` or `write`: bundle uploads and writes on that repository |
| **oidc** | a GitHub Actions job on the public `/v1` routes | the repository its OIDC token proves | nothing |
| **public** | no credentials | only what the public routes answer themselves (webhooks, `/v1/verdict` for public repositories, `/login`) | nothing |

The public `/v1` routes (`/v1/verdict`, `/v1/runs/<id>`, `/v1/runs/<id>/events`, JSON polling and
the event stream alike) answer a signed-in user or a `cst_` token for every repository it may read,
with the full (non-public) view: `log` events and the verdict's reason included. So `coverset-ci
wait` can follow a run with the repository token a CI job already holds for `POST /v1/bundles`
(any scope reads its repository), not only with an OIDC token. A `cst_` bearer bound elsewhere is a
404 there, like a run that does not exist, and one that does not verify a 401, exactly like an OIDC
token for another repository (`web/api.py` `authorize_repo`; `tests/web/test_polish_api.py`).

A repository the principal cannot see answers **404**, with the same words as a repository that
does not exist, so ids cannot be enumerated. A repository the principal can see but may not
change answers 403. Instance-admin pages (`/setup`, `/forge/outbox`, `/settings/instance...`,
`/v1/facts`, `/audit`) answer 403 to everyone else, whatever their repository permissions. The
header lists only the repositories the caller may read, and the dashboard counts only their runs.

### Making someone an admin

* `COVERSET_ADMINS=alice,bob` (comma-separated GitHub logins, matched case-insensitively). When
  the list is set it decides the role at **every** sign-in: listed logins become admins, everyone
  else becomes a member, so removing a login from it demotes that person at their next sign-in.
* `coverset users promote LOGIN` and `coverset users demote LOGIN` change the stored role
  immediately (the next request sees it). When `COVERSET_ADMINS` is set and does not agree, the
  list wins again at the next sign-in; the command says so.
* The admin token is always an instance admin and never appears in `users`.

### Disabling someone

`coverset users disable LOGIN` refuses their sign-ins, deletes their sessions and rejects their
personal tokens from the next request on; `coverset users enable LOGIN` undoes it. Removing the
person from the repository on GitHub is the normal way to take a repository away: the next access
refresh drops the row.

## What a collaborator sees

Someone with `pull` on `acme/app` and nothing on `acme/lib` signs in and sees: the dashboard with
`acme/app` only, its runs, PRs, source views, function pages, tests and invariants; no quarantine
toggle, no Approve button; `/account` with their one repository marked `read`; 404 for every
`acme/lib` URL and for `/runs/<id>` of its runs; 403 for `/setup`, `/forge/outbox` and `/audit`.
With `push` the same person gets the quarantine toggle and may approve untrusted runs of `acme/app`.

If that person later revokes coverset's authorization on GitHub (or the refresh token dies), the
next access refresh gets a 401 or `bad_refresh_token`: coverset deletes their `repo_access` rows,
every session and the stored tokens, records `access_revoked`, and their next request lands on
`/login?reason=revoked` with a note. A GitHub outage or a 5xx leaves the last known access
standing (`/account` says so) and is retried at the next refresh.

## Sessions

* The cookie session (`coverset_session`) is signed with the app's secret key (`COVERSET_SECRET_KEY`,
  else derived from the admin token, else a key file in the data directory), `HttpOnly`,
  `SameSite=Lax`, and `Secure` when `COVERSET_PUBLIC_URL` is `https://`. It carries only the
  server-side session id, the CSRF token and (for the admin token) the admin flag. After a GitHub
  sign-in it is a permanent cookie (`Max-Age` = `COVERSET_SESSION_DAYS`, the row's lifetime:
  `create_app` sets `PERMANENT_SESSION_LIFETIME` next to the `SESSION_COOKIE_*` keys from the same
  `session_lifetime` the row's expiry uses) so it outlives the browser;
  the admin-token cookie stays a browser-session cookie because it has no row to revoke.
* `user_sessions` rows expire after `COVERSET_SESSION_DAYS` (30). `last_seen_at` is written at
  most every five minutes. The row's user must be enabled; a disabled user's sessions are
  rejected at once.
* `/account` lists your sessions (started, last seen, client address, browser, expiry) with a
  Revoke button each; `POST /logout` deletes the current one. `coverset users disable` deletes
  them all. Sessions belong to browsers: `POST /logout`, `/account/refresh` and the revoke
  buttons refuse `cst_` bearers (403), so a leaked read-only token cannot sign you out.
* Every state-changing request from a cookie session needs the CSRF token (`X-CSRF-Token` header
  or the `csrf_token` form field); bearer requests are exempt because browsers never attach that
  header cross-site.
* Behind a proxy set `COVERSET_TRUST_PROXY=true` so the client address in sessions, rate limits
  and the audit log is the caller's, not the proxy's (see `web/proxy.py`).

## Avatars

The header and `/account` show the signed-in user's GitHub avatar (`users.avatar_url`, refreshed
at every sign-in). The CSP's `img-src` allows exactly one third-party origin for it,
`https://avatars.githubusercontent.com` (`app.py` `AVATAR_ORIGIN`), and the templates render a
stored URL only when it starts with that origin and a `/`; anything else (a GitHub Enterprise
host, a lookalike such as `avatars.githubusercontent.com.evil.example`, no avatar) gets the initials
badge instead. The image is requested with `referrerpolicy="no-referrer"`, so GitHub learns the
viewer's address and nothing about the page. In the header a personal API token shows the initials
(the guard loads no user row for a token); its `/account` page shows the image.

## API tokens

A token is `cst_<8-character prefix>_<secret>`; only the sha256 is stored (`api_tokens`), the row
is found by prefix and compared in constant time. The raw token is printed once by the CLI (or
shown once on `/settings/tokens`) and masked by the log redactor everywhere else.

| Kind | Bound to | Scopes | Typical use |
|---|---|---|---|
| personal | a user | `read`, `write`, `admin` | an agent or script acting as you (the MCP server, `coverset-ci`) |
| repository | one repository | `upload`, `read`, `write` | a CI job posting bundles with `POST /v1/bundles` |

Scopes cap what the token's owner could do: a personal `read` token sees the user's repositories
and changes nothing; `write` allows the user's writes; `admin` allows the user's admin powers if
they have any. A repository token with `upload` or `write` holds `write` on its repository and
nothing anywhere else. Expiry (`--expires-days`) and revocation (`tokens revoke`) take effect on
the next request; `last_used_at` is written at most once a minute per token. A `cst_` bearer that
does not verify is a 401 (never a fallback to the cookie) and counts as a failed sign-in for the
client address: beyond 20 rejections a minute the answer is 429 with `Retry-After`, on every
route. It is audited as `token_rejected` with the public prefix at most once a minute per address
and prefix (the row carries how many repeats were `suppressed`); successful uses are not audited.
`/static/*` requests never resolve credentials at all, so a bogus bearer there costs nothing.

```sh
coverset users list
coverset users promote alice
coverset users tokens create --name "ci upload" --repo acme/app --scopes upload --expires-days 90
coverset users tokens create --name laptop --user alice --scopes read,write
coverset users tokens list [--all]
coverset users tokens revoke 3
```

## The audit log

`audit_log` rows: when, actor (`user`, `admin_token`, `api_token`, `oidc`, `system`), action,
target, repository, client address and a details object whose every string went through the log
redactor (`web/audit.py`, `record_audit`). Recorded: `login`, `login_failed`, `logout`,
`session_revoked`, `token_rejected`, `access_refreshed`, `access_revoked`, `role_changed`,
`user_disabled`, `user_enabled`, `token_created`, `token_revoked`, `approve`, `quarantine`,
`setting_changed` (the settings pages), `github_app_stored` and `github_app_forgotten` (setup) and
`secrets_rekeyed` (`coverset secrets rekey`). Instance admins read it at `/audit`
(newest 500, filters by action and repository). The CLI's events carry the actor `cli`.

## Configuration

| Key | Default | Meaning |
|---|---|---|
| `COVERSET_GITHUB_APP_CLIENT_ID`, `COVERSET_GITHUB_APP_CLIENT_SECRET` | unset (the App `/setup` stored provides them) | enable "Sign in with GitHub" when you manage the App's credentials in the environment |
| `COVERSET_ADMINS` | empty | GitHub logins that are instance admins at every sign-in |
| `COVERSET_SESSION_DAYS` | 30 | lifetime of a login session |
| `COVERSET_ADMIN_TOKEN` | unset (loopback only) | the operator's token; required for any non-loopback bind or public URL |
| `COVERSET_SECRET_KEY` | derived | cookie signing key (set it when several replicas serve one URL), and the key of the vault that stores the GitHub App (required for `/setup`) |
| `COVERSET_TRUST_PROXY` | false | take the client address and scheme from the nearest proxy's headers |
| `COVERSET_PUBLIC_URL` | `http://<bind>` | base of the OAuth callback URL; must match the App's callback |
