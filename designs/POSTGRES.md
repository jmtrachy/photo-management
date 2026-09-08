# Postgres migration

I'm starting to model real relationships between business objects (albums, collections, photos, shares) and
looking at multi-tenancy next. DynamoDB's single-table-per-entity + GSI shape is fighting both of those —
`collection_albums` and `memberships` are hand-rolled join tables today, and multi-tenancy means redesigning
every list-style GSI (see [[MULTITENANT.md]]) rather than adding a `WHERE tenant_id = ?` clause. Moving the
relational, cross-object parts of this app onto Postgres makes both of those easier instead of harder as the
object graph grows.

## Approach

Dual-write, then feature-flagged reads, staged exactly like the environment [[STAGING.md]] already built for
this purpose: validate the whole flow in staging first, then repeat in prod.

1. New code writes to both DynamoDB and Postgres behind a `DUAL_WRITE_ENABLED`-style env var, toggleable
   per-stage.
2. A backfill script copies existing rows into Postgres once dual-write is live.
3. A reconciliation script diffs both stores before anything reads from Postgres for real.
4. Reads flip over behind a second flag, one global switch rather than per-entity — albums pull photos via
   `memberships`, collections pull albums via `collection_albums`, so serving related entities from different
   backends mid-request adds consistency risk for no benefit at this scale.
5. Once prod reads are stable on Postgres, stop writing to DynamoDB, then delete the DynamoDB tables.

A dual-write failure should fail the whole request rather than log-and-continue — at this write volume, loud
failure beats quietly drifting the two stores out of sync and having to reconstruct which rows disagree later.

## Hosting

Aurora Serverless v2 with the Data API, not a traditional pooled connection (RDS Proxy + a normal driver). The
Data API is HTTP-based, which fits the "one request per Lambda execution environment, no persistent
connection" model every `database/*.py` module already documents in its own NOTE comment — it changes less
about how the Lambda talks to its datastore. Worth benchmarking Data API latency before committing; RDS Proxy
is the fallback if it's not fast enough.

## Cost

DynamoDB `PAY_PER_REQUEST` at this app's traffic is effectively free — cents a month. None of the Postgres
options are close to that, which matters given CLAUDE.md frames this as a personal/family app, not a product
with paying users yet:

- **Aurora Serverless v2** has a cost floor, not a scale-to-zero: it can't go below 0.5 ACU, and at
  ~$0.12/ACU-hour that's roughly **$43/month idle**, before storage or I/O.
- **RDS** is cheaper on the instance itself (~$12-15/month for a `db.t4g.micro`), but Lambda really wants
  RDS Proxy in front of it to avoid connection exhaustion, and Proxy is billed per vCPU-hour of the
  instances behind it — RDS + Proxy lands in a similar **$30-50/month** range to Aurora Serverless v2 once
  that's added.
- **Self-hosted Postgres on EC2** is the cheapest, ~**$6-8/month** for the smallest instance, but trades
  money for operational burden — patching, backups, and connection pooling all become manual work instead of
  managed.

None of these are worth taking on for their own sake at current traffic. The real trigger for this migration
is demand: whether anyone besides me actually wants to use this site to share photos, versus just using
something like Lightroom they already get for free with a subscription. If that demand shows up, the
relational/multi-tenant case for Postgres and the $30-50/month cost both become easy to justify together. Until
then, this stays a documented option, not a plan with a timeline.

## Schema, grounded in the current 7 tables

- **`albums`** (`database/albums.py`) — `album_id` PK, `title`/`title_lower`, `subjects` (array/jsonb),
  `event_date`, `sort_order`, `cover_photo_id` (FK → photos), `view_count`, `download_count`, `created_at`.
- **`collections`** (`database/collections.py`) — `collection_id` PK, `title`/`title_lower`,
  `cover_album_id` (FK → albums), `share_id`, `view_count`, `created_at`.
- **`collection_albums`** (`database/collection_albums.py`) — today a hand-rolled join
  (`pk=COLLECTION#…`/`sk=ALBUM#…` with a `ByAlbum` GSI for the reverse lookup). Becomes a real join table:
  `(collection_id FK, album_id FK)` composite PK, plus `visibility`, `share_id`, `created_at`. The reverse
  lookup (`list_album_collections`) is just the same table with the FK constraint doing double duty.
- **`photos`** (`database/photos.py`) — `photo_id` PK, `sha256` (unique index — replaces `BySha256`),
  `taken_at`, `uploaded_at`, `view_count`, `download_count`.
- **`memberships`** (`database/memberships.py`) — same shape as `collection_albums`: `(album_id FK,
  photo_id FK)` composite PK, `taken_at`. Sorting by `taken_at` becomes a plain indexed `ORDER BY` instead of
  fetch-everything-then-sort-in-Python (`memberships.py:48`); the `ByPhoto` reverse-lookup GSI becomes the
  same FK relationship queried the other way.
- **`shares`** (`database/shares.py`) — `share_id` PK, `entity_type`, `album_id`/`collection_id` (FK,
  nullable depending on `entity_type`), `created_at`, `view_count`, `zip_status`, `zip_error`, `photo_count`.
  `scan_album_shares` (`database/shares.py:26`) is today a full-table `Scan` filtered client-side by
  `album_id` because Dynamo has no query path for it — becomes an indexed `WHERE album_id = ?`.
- **`tokens`** (`database/tokens.py`) — short-TTL login tokens. Low value to migrate (ephemeral, no
  relationships to anything); could reasonably stay on DynamoDB indefinitely rather than be part of this
  effort at all.

## Ticketed items (in order of execution)

### 1. Schema design & hosting decision
Finalize the Postgres schema above (types, constraints, indexes) and stand up an Aurora Serverless v2 cluster
with the Data API in staging first, matching how every other piece of infra here gets proven out.

### 2. Postgres access layer
A new `database_pg/` package mirroring the existing `database/` module-per-entity structure 1:1 — same
function names, signatures, and return shapes as their DynamoDB counterparts, so `app.py` call sites don't
need to change, only which module they import from.

### 3. Dual-write wrapper + feature flag
Wrap every write function (`create_album`, `set_title`, `add_memberships`, `mark_zip_ready`, etc.) to call
both backends when `DUAL_WRITE_ENABLED` is set. First feature flag in the app — needs the actual plumbing
(env var read, presumably a small helper rather than repeating the check at every call site).

### 4. Backfill script
One-time script, run after dual-write is live (to avoid a race between a backfill read and a fresh write),
copying every existing row across all 7 tables into Postgres.

### 5. Reconciliation script
Diffs row counts and spot-checks content between DynamoDB and Postgres per table, to build confidence before
any reads move over.

### 6. Flip reads — staging first
Turn on the read flag in staging, live with it, confirm nothing's off, then repeat the same sequence
(dual-write → backfill → reconcile → flip reads) in prod.

### 7. Decommission DynamoDB
Once prod reads are stable on Postgres, turn off DynamoDB writes, then delete the DynamoDB tables and related
stack resources.

## Open questions

- **Testing story.** The current DynamoDB tests (`tests/database/`) hand-mock the boto3 client directly
  rather than using something like `moto` — Postgres needs its own equivalent (likely a local/in-memory
  Postgres for the test suite), not a port of the existing mocks.
- **Data API latency.** Needs a real benchmark before committing over RDS Proxy.
- **Is `tokens` worth migrating at all**, given it's ephemeral and has no relationships to anything else here.
