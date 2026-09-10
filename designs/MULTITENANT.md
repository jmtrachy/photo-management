Bottom line: this is a genuinely large, cross-cutting effort — not a handful of PRs. It's not that any single piece is hard, it's that "tenant" isn't a dimension that exists
  anywhere in the current design, so it has to thread through the schema, every index, and essentially every one of the ~40 endpoints in app.py. Here's where the actual work is,
  grounded in what's there today:

  1. GSI redesign — a real schema migration, not just an app-side filter.
  AlbumsTable's ByCreatedAt index and PhotosTable's ByTakenAt index both currently partition on a fixed constant (entity_type = "ALBUM" / "PHOTO") — meaning everyone's
  photos/albums live in one shared partition today. "List my recent albums" only works efficiently because there's implicitly one tenant. Making that per-tenant means the
  partition key becomes something like tenant_id instead of a constant — a real index redesign (and a data migration for anything already deployed), not a WHERE clause you can
  bolt on.

  2. The one that'd actually bite you if missed: dedup hashing. PhotosTable's BySha256 index is used to skip re-uploading a photo that already exists (get_photo_by_sha256 in
  presign_uploads). If that stays a global index, one tenant uploading a photo that happens to hash-match another tenant's photo would silently return the other tenant's photo_id
  — a real cross-tenant data leak, not just a UX bug. That key needs to become tenant-scoped too (e.g. f"{tenant_id}#{sha256}"). Worth flagging first because it's the kind of
  thing that's easy to miss and bad if missed.

  3. The actual bulk of the work: an authorization sweep across every endpoint. Today, require_admin only answers "is this email in the allowlist" — there is zero concept of
  "does this album/photo/collection belong to the caller" anywhere, because there's only ever been one tenant. Every list/get/mutate endpoint (albums CRUD, photos CRUD,
  collections CRUD, uploads, deletes...) would need an ownership check added, either via the tenant-scoped index redesign from #1 or a fetch-then-verify pattern. This is
  mechanical but touches nearly the whole file.

  4. S3 key namespacing. Keys are flat today (originals/{photo_id}.{ext}, derivatives/{photo_id}/{variant}.jpg, zips/{share_id}.zip). Prefixing by tenant
  (originals/{tenant_id}/{photo_id}.{ext}) is mechanical but touches every key-construction site — presign_uploads, _derivative_url, _build_album_zip, and derivatives.py's own
  key derivation.

  None of that is exotic, but together it's a genuine re-architecture, not a feature add — I'd size it in weeks, not days, for a codebase this size, mostly because of #3's
  breadth.

  Before I'd want to scope it more precisely, two things change the size of this a lot:

  - How do tenants get created? A short, manually-provisioned list of family members (extend the ADMIN_EMAILS-style allowlist into a small Users table mapping email → tenant_id)
  is a modest add. A real self-serve signup flow is a materially bigger project (account creation, invites/abuse prevention, etc.) — and CLAUDE.md currently frames this as a
  personal/family app, not a SaaS product, so I'd want to know which one you actually mean.
  - Does data ever need to cross tenants? i.e., is isolation total (my photos are mine, yours are yours, full stop), or do you want in-app sharing/collaboration between tenants
  at some point (not just public share links, which already work fine regardless of tenancy)? That changes whether "tenant" is a hard boundary or just a default scope.

  ## Impersonation ("act as" / ghost user)

  A companion feature: a super-admin screen where the primary user can select any other tenant and view the
  site exactly as that tenant sees it. This depends on the multi-tenant foundation above — there's nothing to
  "act as" until tenant-scoped data and a per-request "who does this belong to" resolution exist, so it should
  be sequenced strictly after that work, not alongside it.

  Once the foundation is in place, the feature itself is small, because it hooks into one existing chokepoint:
  every endpoint already goes through `require_admin` via FastAPI's `Depends()`.

  1. A super-admin concept, separate from "is a valid tenant user" — simplest version is another small email
     allowlist (like today's `ADMIN_EMAILS`, but for "can impersonate" rather than "can log in").
  2. An impersonation overlay on top of the session. The real signed session cookie keeps identifying the
     super-admin — impersonation should not re-authenticate as someone else, so the super-admin's own audit
     trail stays intact. A second, separate piece of state (another small signed cookie, or a server-side flag
     keyed off the session) records "currently viewing as tenant Y." The shared identity-resolution dependency
     (whatever `require_admin`/`get_current_tenant()` becomes under multi-tenancy) checks: real user is
     verified super-admin + impersonation target set → resolve effective tenant as Y instead of their own.
  3. The screen itself — a new super-admin-only page listing all tenants with an "act as" button, plus
     start/stop-impersonating endpoints. Small: a list + a button, reusing existing admin-console patterns.

  Three things to decide going in, not discover later:
  - Persistent "Viewing as: [tenant]" banner while impersonating, with an obvious "stop" action — otherwise
    it's easy to take a destructive action (Fully Delete) thinking you're in your own account.
  - No privilege escalation through it — impersonation lets the super-admin see what a tenant sees, not grants
    tenant-management powers they wouldn't otherwise have while "as" them.
  - At least minimal audit logging (who impersonated whom, when) — cheap now, painful to reconstruct later.

  Effort: modest on its own (roughly a couple of days), but only once the multi-tenant foundation is done —
  before that it has nothing to attach to.

  ## GSI redesign specifics

  The schema-migration item above (#1) splits into two very different categories of change, not one uniform
  pattern:

  ### Category A: point-lookup tables need only a new attribute, no key-schema change

  `PhotosTable`, `AlbumsTable`, `CollectionsTable`, `SharesTable` are all fetched by their own opaque id
  (`get_item(Key={"photo_id": ...})` etc.) — that access pattern doesn't touch a GSI at all, so it needs zero
  index surgery. Add a plain `tenant_id` attribute to every item, and rely on the app-layer authorization
  check (item #3 above — fetch the item, verify `item["tenant_id"] == caller_tenant_id` before
  returning/mutating) rather than the key schema. The base table's primary key stays exactly as-is.

  ### Category B: the "list mine" and dedup indexes need real key-schema surgery

  - **`AlbumsTable.ByCreatedAt`** and **`PhotosTable.ByTakenAt`**: today the GSI partition key is the literal
    constant `"ALBUM"`/`"PHOTO"` (`entity_type`), with `created_at`/`taken_at` as the sort key
    (`database/albums.py:33-38`, `database/photos.py:119-124`) — everyone's items live in one shared
    partition today. Fix: not "add a sort key" (there already is one, and it stays as-is for newest-first
    ordering) — **swap the partition key attribute** from the constant to `tenant_id`. Query changes from
    `Key("entity_type").eq("ALBUM")` to `Key("tenant_id").eq(my_tenant)`; sort key and `ScanIndexForward=False`
    unchanged.
  - **`PhotosTable.BySha256`** (`database/photos.py:59-72`, the dedup check): today partition-key-only on
    `sha256`. Make `tenant_id` the GSI's **sort key** (not a concatenated synthetic string) —
    `KeyConditionExpression=Key("sha256").eq(x) & Key("tenant_id").eq(y)`. This is the actual fix for the
    cross-tenant leak in item #2 above.
  - **`MembershipsTable.ByPhoto`** and **`CollectionAlbumsTable.ByAlbum`**: reverse lookups keyed off already
    opaque, unguessable ids. As long as the Category-A ownership check happens before trusting `photo_id`/
    `album_id` as input to these queries, they don't strictly need `tenant_id` in their key schema — leave
    these alone rather than add it defensively everywhere.

  ### Migration mechanics

  DynamoDB GSIs are immutable once created — no in-place key-schema change. For each Category B index: create
  a **new** GSI (new name) with the corrected key schema, backfill the `tenant_id` attribute onto every
  existing item (a GSI only indexes items that have its key attributes present), wait for it to finish
  building, cut the app's queries over to the new index name, then delete the old one. That backfill script is
  the one genuinely non-trivial data-migration piece — everything else is additive.

  ### Freebie while touching this

  `SharesTable` currently has no GSI at all — `scan_album_shares` does a full table `Scan` filtered
  client-side by `album_id` (`database/shares.py`), which already doesn't scale and gets strictly worse under
  multi-tenancy (scanning every tenant's shares to find one album's). Not required for tenancy itself, but
  worth adding a proper `ByAlbum` GSI (`album_id` as partition key) and dropping the scan while this table is
  already getting touched.