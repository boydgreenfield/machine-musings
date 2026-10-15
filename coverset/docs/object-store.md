# Object store

Everything coverset keeps that is not a database row is a blob: the Parquet facts of every run,
each run's manifest, each repository's id dictionary, production-flow batches, sources uploaded with
`coverset-ci upload --with-sources`, and upload archives waiting to be processed. They live in one
blob store: files under the data directory by default (`LocalBlobStore`, so a laptop needs no
service besides Postgres), or any S3-compatible bucket when `COVERSET_BLOB_URL` is set (AWS S3,
MinIO, Cloudflare R2, Tigris). Either way a disposable local cache sits in front of it and the same
code runs above it: manifests, conditional writes, pulls and publishes, eviction. With a bucket and
Postgres ([postgres.md](postgres.md)) the control plane is stateless: nothing under the data
directory has to survive a restart (`Settings.hosted`), and several replicas can serve the same
state ([deploy.md](deploy.md)).

Code: `src/coverset/storage/` (the `BlobStore` protocol, `LocalBlobStore`, `S3BlobStore`, the cache,
upload archives), `facts/store.py` (manifests, pull and publish), `facts/dictionary.py`
(compare-and-swap), `facts/sources.py` (`SourceStore`), `facts/flows.py`, `facts/gc.py`.
Tests: `tests/storage/`, `tests/facts/` (every test that takes the `store` or `replicas` fixture runs
over local files and over S3), `tests/web/test_blob_uploads.py`, `tests/e2e/test_hosted_facts.py`.

## Configuration

| Setting | Default | Meaning |
|---|---|---|
| `COVERSET_BLOB_URL` | empty: files under `COVERSET_DATA_DIR` | `s3://bucket[/prefix][?endpoint_url=...&region=...]` |
| `COVERSET_CACHE_DIR` | `<data_dir>/cache` | the local cache: facts, git mirrors, fetched sources; always safe to delete (mode 0700, like the data dir: both hold private source) |
| `COVERSET_CACHE_MAX_GB` | `10` | the cache's bound (decimal GB); gc and each process's first downloads evict beyond it |
| `COVERSET_BLOB_SYNC_S` | `5` | how long a reader trusts its last check of a run's manifest |

The URL forms:

| Store | `COVERSET_BLOB_URL` |
|---|---|
| AWS S3 | `s3://my-bucket/coverset?region=eu-west-1` (no `endpoint_url`) |
| MinIO (the dev stack in `deploy/dev-compose.yml`) | `s3://coverset?endpoint_url=http://127.0.0.1:9000&region=us-east-1` |
| Cloudflare R2 | `s3://coverset?endpoint_url=https://<account id>.r2.cloudflarestorage.com&region=auto` |
| Tigris (Fly) | `s3://coverset?endpoint_url=https://fly.storage.tigris.dev&region=auto` |

The optional path after the bucket is a key prefix (so one bucket can hold several instances).
Credentials never go in the URL (one with `user:pass@` is refused at start-up): boto3's own chain
reads `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` (`AWS_SESSION_TOKEN`), `AWS_PROFILE`, or the
instance or task role. With an endpoint, requests use path-style addressing and send checksums only
where an operation requires one (botocore's newer CRC32-on-every-upload default is refused by some
S3-compatible stores). Requests retry in botocore's `standard` mode (5 attempts, back-off on
throttling, 5xx and connection errors), with a 5 s connect and a 60 s read timeout. Objects above
8 MiB go up in 8 MiB parts.

`Settings.facts_dir` is `<cache_dir>/facts` and `Settings.mirrors_dir` is `<cache_dir>/mirrors`:
both are caches, whatever holds the blobs (the test-economy pass finds a repository's mirror beside
the facts directory, `facts/economy.mirror_history`).

`coverset deploy check` has an `object store` row (`coverset.storage.check_store`): it creates a
throwaway object under `_coverset/probe/` with `If-None-Match: *`, tries to overwrite it with a
wrong `If-Match` (which must be refused), reads it back, lists and deletes it, on the local store
and on a bucket alike.

## Keys

```
facts/repo=<id>/dictionary.json                              the repository's ids (compare-and-swap)
facts/repo=<id>/run=<id>/MANIFEST.json                       what the run contains (written last)
facts/repo=<id>/run=<id>/<table>.v<n>.<tag>.parquet          one version of a run table, summary or economy table
facts/repo=<id>/run=<id>/<table>.shard=<s>[.attempt=<k>].v<n>.<tag>.parquet   one version of a shard table or ingest log
facts/repo=<id>/prod/service=<s>/batch=<stamp>-<sha12>/<table>.parquet   production windows
sources/<repo_id>/<sha>/<path>                               uploaded sources of a commit
uploads/<repo_id>/<sha>/<label>.tar.gz                       an upload's shard, for the processing pass
```

In the cache a run file keeps its plain name (`runs_dim.parquet`,
`test_lines.shard=0.attempt=2.parquet`); in the store each *version* of it is its own object, named
with the manifest version that introduced it and a random tag (`runs_dim.v8.3f9a1c2e.parquet`), and
the manifest maps the name to the object. With the local store the keys are paths below the data
directory (`<data_dir>/facts/repo=1/run=.../MANIFEST.json`). An
object is never rewritten in place (why: *Consistency rules* below). Every key is checked (`storage.base.check_key`): the facts store's
path rules (relative, normalised, no `..`, no backslash, no NUL), no control characters, no segment
shaped like a local temporary file, at most 1,024 UTF-8 bytes. Unicode and spaces are kept as they
are (a source file may be called `naïve file.py`); keys built from untrusted input (an uploaded
source path, an upload label) pass the caller's own check first.

## Facts over the blob store

The facts store runs on local files: DuckDB, pyarrow, the stamps, the hidden-temp atomic writes and
every query read the cache. The store holds the truth through one object per run:

```json
{
 "format": 2, "repo_id": 1, "run_id": "run_01M4X7...", "version": 7,
 "created_at": "2026-10-14T11:58:03+00:00",
 "written_at": "2026-10-14T12:00:00+00:00", "writer": "web-2:4182",
 "files": {
  "file_cov.parquet": {"key": "file_cov.v7.9a01c4e2.parquet", "etag": "9b2cf5...",
                       "mtime_ns": 1791993600123456789, "size": 2210},
  "ingest_log.shard=0.parquet": {"key": "ingest_log.shard=0.v7.0b7d3e11.parquet", "etag": "41d0c4...",
                                 "mtime_ns": 1791993599012345678, "size": 1904}
 }
}
```

`created_at` is `runs_dim.created_at`, copied in whenever `runs_dim` is published, so retention can
age a run from the manifest alone.

**Publish.** Writers of a run already serialise on the run lock (`FactsStore.run_lock`: ingest of a
shard, a failure record, finalize). Taking the lock *pulls* the run (below), so a writer always starts
from the published version. Releasing it publishes: every local `*.parquet` that changed since the
last pull or publish (size, mtime or inode differs from the hidden `.sync.json` record) is uploaded
as a **new object** (`<name>.v<version>.<tag>.parquet`), 16 at a time; then the manifest is written
**last**, with `If-Match` on the etag the writer pulled (`If-None-Match: *` for a new run); only then
are the objects the new manifest no longer references deleted (the previous versions of the files
that changed, and the files the run dropped: superseded attempts, aggregates an ingest invalidated).
A lost race (`If-Match` fails) deletes the objects this writer just uploaded, since nothing can
reference them, and raises `RunWriteConflict`. A write outside the lock (tools, tests) publishes at
once. The version only grows.

**Pull.** A reader looks at a run through `FactsStore.run_dir`, `_names` (every listing) and `stamp`
(every lease and memo key). Each first checks the run: a `HEAD` of the manifest, at most once per
`COVERSET_BLOB_SYNC_S` per run and process (within that window the check is one `stat` of
`.sync.json`). When the etag differs from the one in `.sync.json`, it `GET`s the manifest, downloads
every file whose etag changed (16 at a time) into hidden temporaries, gives each the mtime its writer
recorded (so `queries.summary_is_fresh`, which compares mtimes, keeps its meaning), renames them into
place, and deletes cached files the manifest no longer lists. A reader pulls only when it can take the
run's lock without waiting; when a local writer holds it, the reader reads the cache as it is (that
writer is producing the newer version). A check that fails (the store unreachable, a manifest naming
an object that is gone) is not retried before the interval has passed either: a reader that holds a
copy keeps reading it (and logs a warning), one with nothing cached gets the error again at once; so
a broken run or a store outage costs one attempt per interval and process, never one per request. A repository's dictionary is pulled the same way (and at once
after a pull that downloaded something, since the new files may use new ids). Because every cache in
`facts/cache.py` and `web/loaders.py` is keyed by stamps of the local files, and a download is a new
inode, the next lookup after a pull misses.

**Consistency rules.**

- A reader never sees a half-written run, whatever happens to the writer. Every version of a file is
  its own object and nothing a published manifest references is deleted until the manifest that
  replaces it has landed, so the version a reader's `GET` of the manifest names is always complete
  in the bucket. A writer that dies after its uploads and before the manifest (a deploy replacing
  the machine, an OOM kill, SIGTERM in a finalize) leaves the previous version exactly as it was:
  every replica keeps reading it, another host can take the run's lock and ingest or finalize it
  (its pull starts from the published version; the dead writer's host, if it comes back with
  unpublished files, has them discarded by its next pull), and gc still ages it. What the dead writer
  uploaded stays in the run's prefix, unreferenced, until the run is deleted (`delete_run` removes
  the whole prefix); it costs space, never correctness.
- A pull sees the manifest replaced under it only when a publish lands between its `GET` of the
  manifest and its downloads: an object it was about to fetch is now deleted (superseded). The reader discards the whole pull and reads the new manifest after
  0.25, 0.5 and 1 s. If three fresh manifests in a row still name an object that is missing or does
  not match, that is not a race (objects are never rewritten) but an object deleted or replaced
  behind coverset's back, and the reader raises `RunWriteConflict` (remembered for the sync interval
  like any failed check).
- No update is lost. Two writers that both pulled version 7 cannot both publish version 8: the second
  `If-Match` fails and raises `RunWriteConflict`; the run keeps the first writer's version and the
  loser's local changes are discarded by its next pull. One process ingests a given run at a time:
  the runner claims a run in the database (one claimant), the upload worker runs one pass per run,
  and the ingest pool's processes on one machine share the cache directory and its fcntl run lock.
- Freshness: a replica that did not write a run sees a new version within `COVERSET_BLOB_SYNC_S`
  seconds of its publish (5 by default; 0 checks on every lookup, about 1.7 ms of `HEAD` per run on a
  loopback MinIO). The writer's own process sees it at once.
- A run whose first publish never happened (a crash before it) has no manifest: readers do not see it,
  and gc removes its objects with the run's prefix. Its age for retention is its newest object's.
- Retention never downloads a run to age it: `facts.gc.expire_runs` reads each candidate run's
  manifest (one small `GET`), takes `created_at` from it, and falls back to the manifest's own
  modification time (`HEAD`) for a run that never got a `runs_dim` row. A gc pass on a leader with an
  empty cache therefore costs one request per run, not the bucket's contents
  (`test_gc_ages_runs_from_their_manifests_without_pulling_them`).
- Clocks: summaries are fresh when written after their sources. If the finalize ran on a host whose
  clock is behind the host that ingested a shard, the pages recompute the answer from `test_lines`
  (correct, slower). Keep NTP on.

**Dictionary compare-and-swap.** `RepoDictionary.extend` over a bucket GETs `dictionary.json` with its
etag, appends the new paths, node ids and shape keys, and PUTs it with `If-Match` on that etag
(`If-None-Match: *` for the first one). A lost race (412, or 409 when two land together) starts the
loop again from the newer version after a random back-off (at most 50 rounds). An id is used only
after the PUT that assigned it succeeded, so two replicas never hand out the same id
(`test_two_writers_never_hand_out_the_same_id`). A `flock` in the cache is held around the loop so
processes on one machine queue instead of racing. The cached copy is replaced only by a version
with at least as many entries.

**The local store emulates S3's conditions.** `LocalBlobStore` evaluates `If-Match` and
`If-None-Match: *` under an exclusive `flock` on `<data_dir>/.blobstore.lock`, so the manifest and
dictionary compare-and-swap is the same code on files as on a bucket. A published file is a hard
link to its object when the cache and the data directory share a filesystem, so the two hold one
copy of the bytes (a download is always a copy); its etag is the inode, size and mtime.

**An ingest worker finds the store from its job.** The ingest pool's worker processes get a
picklable job with the facts directory and the blob URL (`IngestJob.blob_url`,
`storage.blob_url_of(settings)`: `COVERSET_BLOB_URL`, or `file://<data_dir>`), and build the same
`FactsStore` the app has; `storage.store_from_url` keeps one store object per URL and process.
Every caller that has settings builds its store with `facts.store.facts_store(settings)`.

## The other blobs

| What | Where |
|---|---|
| Production batches (`coverset flows import`) | `facts/repo=<id>/prod/...`; a batch is immutable, its `prod_windows.parquet` is uploaded last and readers mirror only complete batches (downloaded whole into a hidden directory, then renamed) |
| Uploaded sources | `sources/<repo>/<sha>/<path>`; every upload is checked into a private staging directory first (the allow-list and path rules), then each file is PUT with `If-None-Match: *` so the first upload of a path wins across replicas too; reads go through `<cache_dir>/sources/...` (0700 directories, 0600 files, no symlink followed): a miss is one `GET` bounded by the 4 MiB source limit from the response's size (no `HEAD`), and a path the store does not have is remembered as missing for `COVERSET_BLOB_SYNC_S`; a whole commit (the upload pass's function index) is one listing, whose sizes skip oversized files, then the `GET`s 16 at a time |
| Upload archives | unpacked into `<runs_dir>/<run_id>/upload/shard-<id>/` (scratch) and kept as `uploads/<repo>/<sha>/<label>.tar.gz`; a pass that finds a shard's directory missing (a restart, another replica) restores it from the archive, under the same untrusted-bundle limits, and only when the archive's recorded sha256 is the one the shard row was registered with |
| Git mirrors | `<cache_dir>/mirrors/<repo>.git`; a run re-clones a missing one as always, and pages re-clone on demand (`orchestrator/repos.restore_mirror`: the source view waits for the clone, a PR page's diff waits up to 10 s (`web/loaders.diff_mirror`) and renders without it after that, other run pages start it in the background; one clone per repository and event loop at a time, a failure retried after 5 min). A diff analysed without the mirror, or one git failed on, is never cached (`web/pr_view`), so the next request shows the changed functions once the clone is done |
| Run scratch (args files, pulled bundles) | `<runs_dir>/<run_id>/`: ephemeral |

**What is ephemeral**: the whole cache directory (facts copies, mirrors, fetched sources) and the
runs directory. Losing them costs downloads (a run's files, a few hundred KB) and one clone per
repository. What must survive is the blob store (the bucket, or the data directory's `facts/`,
`sources/` and `uploads/`) and the database.

## The cache

`COVERSET_CACHE_MAX_GB` (10 by default) bounds the cache (`storage/cachedir.py`). Eviction removes
whole entries, least recently used first: a run directory (last used: its last pull or publish), a
mirror (last fetch), a commit's fetched sources. It never removes an entry in use: a run whose lock
is held (an ingest, a pull), a run with changes not yet published (no `.sync.json`, or files that
differ from it), a mirror whose fetch lock is held. It runs in `coverset gc` (which `serve` runs at
start and every 24 h, on the replica holding the leader lease) and, because every replica has its
own cache, in each process after its first download and then at most every 10 minutes. An evicted
run is pulled again by its next reader. With the local store a published run's cached files are
hard links to its objects: they count toward the bound, though evicting them frees no disk until
the objects go too.

```sh
uv run coverset cache status            # size by kind against the bound, entries in use
uv run coverset cache clear --yes       # remove everything not in use (always safe)
```

## Retention and purge

`coverset gc` ages runs from their manifests (one `GET` each, no download) and deletes the expired ones
with `FactsStore.delete_run`: the manifest first (readers stop seeing the run at once), then every
object under the run's prefix (which also removes objects left by a writer that died mid-publish),
then the cached copy. The uploaded
sources and the upload archives of a commit go once no remaining run of the repository has that head
(`orchestrator/history.prune_upload_files`). `coverset flows prune` deletes a batch's objects, then its
cached copy. `coverset repo purge` (`facts/gc.purge_repo_files`) deletes `facts/repo=<id>/`,
`sources/<id>/` and `uploads/<id>/` from the store, and the cached copies, the mirror and the runs'
scratch directories.

## Bucket policy and permissions

The bucket is private: no public access, no website hosting. coverset needs, on its prefix:

| Permission | For |
|---|---|
| `s3:ListBucket` (on the bucket) | listing runs, sources, batches; without it S3 answers 403 instead of 404 for a missing key |
| `s3:GetObject` | pulls, sources, archives |
| `s3:PutObject` | publishes, the dictionary, uploads; conditional writes need nothing more |
| `s3:DeleteObject` | superseded files, gc, purge |
| `s3:AbortMultipartUpload` | cleaning up a failed upload of a file above 8 MiB |

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
     "Resource": "arn:aws:s3:::coverset-prod"},
    {"Effect": "Allow",
     "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload"],
     "Resource": "arn:aws:s3:::coverset-prod/coverset/*"}
  ]
}
```

(`deploy/aws/iam-policy.json` is this policy.) Add `kms:Decrypt` and `kms:GenerateDataKey` on the key
for SSE-KMS. Recommended on the bucket: Block Public Access, default encryption, versioning (a
deleted run can be recovered while the database still has its row), and a lifecycle rule that aborts
incomplete multipart uploads after a day. On R2 and Tigris use a token scoped to the bucket with
object read and write; on MinIO a user with a policy like the one above.

**Conditional writes.** The dictionary and the manifests rely on the store evaluating `If-Match` and
`If-None-Match: *` on `PutObject` atomically. AWS S3 (since 2024), MinIO and Cloudflare R2 do; Tigris
documents conditional writes too, and `coverset deploy check` confirms it on the real bucket. A store that ignores them would turn every compare-and-swap into a blind overwrite: two replicas
could then hand out the same dictionary id or drop each other's run versions. `coverset deploy check`
probes for it and warns; on such a store run exactly one replica (the single-writer case is safe
whatever the store does), or put the instance on a store that supports them.

## Measured

On the local demo (seeded repository, six baselines, both PRs; 20 to 30 small Parquet files per
run), on an Apple M4 Max laptop, with a harness that seeds the demo once per bucket through a
process ingest pool and then drives the app's test client (2026-10-14), two buckets: moto running in
the same process (it shares the GIL with the app, so it is the slow end), and the compose MinIO on
loopback (a real server over HTTP). "Cold" is a page's first request with every in-process cache
emptied and an empty facts cache (a new replica: the page pulls what it reads); "warm" is the p50
and p95 of 30 requests after 3 more.

| Page | moto cold / p50 / p95 (ms) | MinIO cold / p50 / p95 |
|---|---|---|
| dashboard `/` | 418 / 8.1 / 9.0 | 197 / 6.9 / 8.6 |
| repository | 419 / 8.3 / 9.4 | 217 / 7.7 / 8.5 |
| PR #2 | 254 / 13.5 / 15.1 | 174 / 11.3 / 12.0 |
| source view | 248 / 3.4 / 4.1 | 155 / 2.9 / 3.2 |
| economy | 217 / 6.0 / 6.6 | 103 / 4.4 / 4.8 |
| map | 121 / 4.8 / 5.2 | 62 / 3.6 / 3.8 |
| tests | 15 / 7.0 / 7.8 | 12 / 5.8 / 6.1 |

Within the sync interval a run's check is one `stat` of its `.sync.json`; with
`COVERSET_BLOB_SYNC_S=0` it adds a `HEAD`, 1.7 ms on loopback. The cold hit pays the downloads: the
dashboard and the repository page read every baseline of the trend (all runs, pulled 16 files at a
time), a PR or source page one or two runs. Against AWS S3 in-region add a few tens of milliseconds
per round trip to the cold numbers; the warm ones do not change.

| Ingest of the demo's real bundle (one shard; median of 5) | moto | MinIO |
|---|---|---|
| `ingest_shard` (publish included) | 48 ms | 27 ms |
| `finalize_run` (publish included) | 94 ms | 70 ms |
| first pull of the run on another replica | 45 ms | 19 ms |
| the whole demo (`run_demo`, tests executed) | 19.3 s | 19.3 s |

The whole demo is dominated by running the tests; the object store adds well under a second to it.

**The local store** (files under the data directory, the default), measured on the same laptop
(2026-10-14, the minimal fixture bundle of `tests/facts`, 9 runs, medians): `ingest_shard` 14 ms
and `finalize_run` 57 ms, publish included (a publish hard-links each changed file into the store,
then writes the manifest under the store's lock); a run's `stamp` within the sync interval 85 µs,
160 µs when every lookup checks the manifest (`COVERSET_BLOB_SYNC_S=0`, one `lstat` of the object);
the first summary query of a run 29 ms. Nine runs take 1.2 MB of disk, cache and store together
(the cache's files share their inodes with the objects).

## Testing against a real store

Unset, the suite's object-store tests use moto in-process (`coverset.storage.testing`: one
`ThreadedMotoServer` per xdist worker on a free port, with `PutObject` serialised so moto's
conditional writes are atomic like a real store's) and everything else uses the local store.
`COVERSET_TEST_BLOB_URL` moves the whole suite onto a real store:

* the object-store tests (`tests/storage`, the `s3` half of `tests/facts`, the hosted e2e) use it
  instead of moto;
* the `settings` fixture (`tests/conftest.py`) gives every app, CLI and orchestrator test a fresh
  key prefix of it as `blob_url`, unless the module brings its own `data_dir` or `blob_url`;
* the page and report tests' shared demo (`tests/web/conftest.py`, `tests/report/conftest.py`) is
  seeded into a prefix of it, so every page test reads its facts through the cache and the bucket.

```sh
docker compose -f deploy/dev-compose.yml up -d --wait
COVERSET_TEST_BLOB_URL='s3://coverset?endpoint_url=http://127.0.0.1:9000&region=us-east-1' \
AWS_ACCESS_KEY_ID=minioadmin AWS_SECRET_ACCESS_KEY=minioadmin \
  uv run pytest -q -n 6 -p no:cacheprovider
```

Every test works under its own key prefix and deletes it afterwards (the bucket held nothing from
the suite after a full run).
