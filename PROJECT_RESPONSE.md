# Photo Management App — Talking Points

Notes on what the app could look like, based on PROJECT.md (ignoring "Future plans").
These are conversation starters, not decisions.

## 1. Fit with the existing `jamestrachy.com` platform

Your `url-shortener` and `qr-code` apps share a consistent shape: **FastAPI + Mangum on
Lambda (Docker image), DynamoDB for state, CloudFront in front of API Gateway, Route53
alias, shared SSM params for hosted zone + wildcard cert**. Strongly recommend following
the same mold here so `photos.jamestrachy.com` slots in next to the others with no new
operational patterns.

- Stack name: `PhotoManagementStack`
- Domain: `photos.jamestrachy.com` (alias via the same wildcard cert + hosted zone SSM params)
- CDK entrypoint: `cdk_app.py`, stack in `photo_management_stack.py` — mirrors url-shortener

## 2. Core domain model

Five logical entities. DynamoDB single-table is probably overkill for this scale — a
few tables keeps it readable:

- **Photo** — `photo_id`, `s3_key`, `uploaded_at`, `original_filename`, `width`,
  `height`, `size_bytes`, `taken_at` (EXIF `DateTimeOriginal`, falls back to
  `uploaded_at`; kept top-level because it's a sort key), `exif` (map:
  `camera_type`, `iso`, `aperture`, `shutter_speed`), `view_count`, `download_count`
- **Album** — `album_id`, `title`, `title_lower` (for substring search),
  `description`, `cover_photo_id`, `created_at`, `view_count`
- **Membership** — the photo↔album many-to-many. One item per (photo, album) pair:
  - PK: `ALBUM#<album_id>`, SK: `PHOTO#<photo_id>`, plus `taken_at` for ordering
  - GSI with PK: `PHOTO#<photo_id>`, SK: `ALBUM#<album_id>` for the inverse direction
  - Optionally denormalize `thumbnail_key` onto the membership so the album grid
    renders from a single Query with no follow-up `BatchGetItem`
- **Share** — `share_id`, `album_id`, `name` (admin-facing label, e.g.
  "Sent to Jane", "Vermont trip — public"), `created_at`, `view_count`
- **PhotoTag** — the photo↔tag many-to-many. One item per (photo, tag) pair:
  - PK: `TAG#<tag_name>` (lowercased), SK: `PHOTO#<photo_id>`, plus `taken_at` for grid ordering
  - The Photo item also carries a `tags` String Set so the detail view renders the tag list from a single GetItem — no inverse Query needed

**Why memberships instead of an `album_ids` Set on Photo:** DynamoDB GSI partition
keys must be scalars, so a Set attribute can't be inverted with a GSI. Adjacency-list
memberships are the canonical many-to-many pattern — tag/untag is one `PutItem` or
`DeleteItem`, "list photos in album X" is one Query against the base table, "list
albums for photo Y" is one Query against the GSI. No duplication to keep in sync.

**Tagging works the same way — separate `PhotoTags` table for the same reasons.**
Querying "all photos with tag X" is one `Query(PK=TAG#<name>)` followed by a
`BatchGetItem` for the photo rows. Tag/untag is a `TransactWriteItems` that
writes/deletes the `PhotoTags` row, mutates the Photo's `tags` Set, and toggles
an `untagged_marker` attribute on the Photo — three writes that succeed or fail
together. So tagging a photo with three tags is three new `PhotoTags` rows plus
the Photo update in a single transaction. The marker exists only when the Set is
empty; a sparse GSI on the Photo table keyed on `untagged_marker` makes the
"Untagged" view a single `Query` (tagged photos drop out of the index
automatically). Canonicalize tag names to lowercased + trimmed on write; if you
want to preserve original casing for display, store it as a separate attribute
on the `PhotoTags` row.

**View tracking.** Three counters capture public engagement, all incremented only
on non-admin traffic (admin sessions detected by auth token/cookie and skipped):

- `Photo.view_count` — bumped when a public visitor opens a photo's detail page
- `Album.view_count` — bumped when a public visitor opens an album share link
- `Photo.download_count` — bumped when a public visitor hits the download-original endpoint

Each increment is a single `UpdateItem ADD … 1` — atomic, no read-before-write.

**Pagination.** Album and photo lists paginate via DynamoDB's `LastEvaluatedKey`
cursor, not offset/page-number. The frontend holds the opaque cursor returned by
the previous page and passes it back as `?cursor=...` for the next page. The API
contract should accept and return this cursor verbatim — don't translate it into
anything more human-friendly.

**Album name search.** The "Add to album" modal (Story 4) needs substring match —
`scr` matches `Soccer 2023`, not just prefix. At personal scale (dozens to low
hundreds of albums) a Scan with `FilterExpression = contains(title_lower, :q)`
is fine. Store a denormalized lowercase `title_lower` alongside `title`,
lowercase the query server-side, scan. Cost scales with total album count, not
match count — revisit around ~1000 albums (paginated scans, an external search
index, or just accept the latency).

## 3. Storage — S3 + derivatives

- **Original** photos in S3 (private bucket, server-side encryption, lifecycle to IA
  after 90d if cost matters)
- **Derived sizes** generated on upload: thumbnail (~300px), medium (~1200px), full
  (original). Store in same bucket under `derivatives/<photo_id>/{thumb,medium,full}.jpg`
  — convention-based, deterministic from `photo_id`, so derivative keys don't need
  to live in DynamoDB. Frontend constructs the CloudFront URL from `photo_id` alone.
- Serve derivatives via CloudFront signed URLs (cheap, cached, expire-able). Originals
  only served via an authenticated admin path or explicit "download original" action —
  which is where your **download tracking** hooks in.

**Shipped diverges:** both derivatives and originals are currently served via S3 presigned
URLs (1-hour TTL) generated per request, not CloudFront signed URLs. This means the URL
changes every page load and the browser can't cache the image across navigations — a known
caching cost flagged for a follow-up (move `derivatives/*` to a public-read CloudFront
behavior backed by Origin Access Control, keep `originals/*` presigned + tracked).

## 4. Public viewer (`photos.jamestrachy.com/a/<share_id>`)

- Static-ish React or plain HTML/JS served from S3 + CloudFront, OR rendered by the
  Lambda. Given the single-digit-request scale you're likely at, Lambda-rendered is
  simplest and matches the existing apps.
- Resolves `share_id` → album, renders a grid of thumbnails, click opens full-size
  with a download button. Album title shown prominently.
- **"Photos added after link created" works naturally** because the share resolves to
  an `album_id`, not a frozen list of photos. New photos tagged into that album show
  up on the next page load.

**Shipped:** Lambda-rendered HTML via `public_album.html` (grid) and `public_photo.html`
(single-photo viewer at `/a/{share_id}/{photo_id}`). Photo viewer is full-bleed with
translucent prev/next arrows scoped to the album's `taken_at`-desc order, picks
medium-size on viewports < 1024px and full-res above, and offers a "Download Full Res
image" text link (not a button) below the image.

## 5. Shareable link flow

**Approach:** photos owns the share end-to-end. Two endpoints split the
concern:

- `GET /s/<share_id>` — share entry point. Atomically increments
  `Share.view_count`, returns 302 to `/a/<share_id>`.
- `GET /a/<share_id>` — public viewer (see §4). Atomically increments
  `Album.view_count`, resolves `share_id` → `album_id`, renders the album.

The shared URL (pasted into texts, emails, etc.) is
`https://photos.jamestrachy.com/s/<share_id>`. After the click the recipient's
URL bar shows `/a/<share_id>`.

**Shipped diverges:** the `/s/` indirection was dropped — only `/a/{share_id}` exists,
and `POST /api/albums/{id}/shares` returns the `/a/` URL directly. `Share.view_count`
is reserved in the schema but never incremented; only `Album.view_count` bumps when the
public viewer loads. Slugs are 8 chars (not 6) over `[a-zA-Z0-9]`. The admin create
endpoint takes no `name` field yet — the share row is identified by slug + `created_at`.

**Why two endpoints / why the 302.** `Share.view_count` measures
link-distribution reach; `Album.view_count` measures actual album views. These
differ in useful ways — bookmarks, refreshes, and bot prefetches hit `/a/`
without going back through `/s/`, so the counters tell different stories.
Keeping them as separate concerns means each endpoint stays simple and atomic.

**Share IDs.** 6-char alphanumeric, generated server-side via `secrets.choice`
over `[a-zA-Z0-9]` — same pattern as `url-shortener/app.py:31-32`, copied not
imported. ~56B possibilities; collisions are a non-event at personal scale, but
a conditional `PutItem` with retry (also borrowed from `url-shortener/app.py:41-55`)
handles them defensively for free.

**Creation.** Admin console: `POST /admin/shares {album_id, name}` →
`{share_id, public_url}` where `public_url` is the `/s/` form. The `name` is
an admin-facing label so the share list is browsable ("which link did I send
to whom?"). Admin copies `public_url` to wherever they're sharing it.

**Listing.** "All shares for album X" via Scan + `FilterExpression` on
`album_id`. At dozens-of-shares-per-album scale, fine — same reasoning as the
album name search in §2. Add a GSI later if it ever matters.

**Why not integrate with url-shortener.** Earlier drafts of this section tried
two integrations: (a) a CloudFront `/u/*` behavior proxying to
`l.jamestrachy.com`'s APIGW, and (b) a per-request logger writing into
url-shortener's table from the photos Lambda. Both fell apart against the
constraint that photos owns `Share` and url-shortener stays opaque:
- (a) removed photos from the request path entirely, leaving `Share` with no
  `view_count`, no disable hook, and no place to attach metadata like `name`.
- (b) split the source of truth — photos' counter and url-shortener's
  `hit_count` would drift, and disable/metadata still couldn't live inside
  url-shortener's schema.

The clean separation: url-shortener remains a standalone general-purpose tool
for other apps and ad-hoc external links; photos implements its own share
concept (a few dozen lines, mostly copied from url-shortener) so it can own
the product surface end-to-end.

## 6. Download tracking

Two places a download can happen:
- **Thumbnail/medium view** — don't count these, they're just browsing.
- **"Download original"** button — route through `GET /d/<photo_id>` on the Lambda,
  which increments `download_count` on the photo record, then 302s to a short-lived
  S3 pre-signed URL.

That same `/d/` endpoint could log structured events (like url-shortener does) so you
could later build a little dashboard.

**Shipped diverges:** the download route is `GET /api/public/shares/{share_id}/photos/{photo_id}/download`
(scoped to a share, not a naked photo_id). Same behavior — atomic `ADD download_count :1`,
then 302 to a presigned URL with `Content-Disposition: attachment`. Logs a single
`public_photo_downloaded` structured event per call. No admin-side download endpoint exists yet.

## 7. Admin console

This is where most of the product complexity lives. Two sub-questions:

**Auth: magic link via SES.**

Email-based passwordless sign-in with a hard-coded allowlist (currently
`jmtrachy@gmail.com`, `agentjimbo@gmail.com`). Flow:

1. Visitor hits `/login`, submits their email
2. If on allowlist, Lambda generates a 32-byte URL-safe token, stores it in a
   `LoginTokens` DynamoDB table with a 15-minute `expires_at` (DynamoDB TTL
   sweeps stale rows for free), and sends a sign-in link via SES from
   `noreply@jamestrachy.com`
3. Click → `/login/verify?token=...` → Lambda atomically reads-and-deletes the
   token (one-time use), then sets a signed session cookie
   (`itsdangerous.URLSafeTimedSerializer`, HMAC-SHA256, 30-day max-age,
   HttpOnly + Secure + SameSite=Lax)
4. Subsequent requests carry the cookie; a FastAPI dependency verifies the
   signature and that the email is still on the allowlist

Whether or not the submitted email is on the allowlist, the form returns the
same "check your email" page — mild anti-enumeration.

**Why not the alternatives.**
- *HTTP basic auth* (the original plan) was tried and dropped: API Gateway REST
  API renames `WWW-Authenticate` to `x-amzn-remapped-www-authenticate`,
  suppressing the browser's auth dialog. The workaround layer (a CloudFront
  viewer-response Function to rename the header back, or switching to a Lambda
  Function URL) wasn't worth the complexity once we admitted basic auth's UX
  is rough anyway — no logout, no remember-me, no path to a second admin.
- *Cognito user pool* — way over-spec for two admins. Adds a managed service
  with its own surface area (hosted UI, user pool config, app clients) for a
  problem we don't have.
- *Sign-in with Google OAuth* — comparable UX to magic-link, but couples us to
  Google and a one-time OAuth-client setup in their console. Magic-link stays
  fully self-hosted and trivially extends the allowlist (just edit the env var).

**Cost / setup notes.**
- SES free tier covers tens of thousands of sends/month — effectively free
  forever at admin-login volumes.
- New AWS accounts start in SES sandbox (recipients must be verified). Each
  allowlist email is verified once with `aws ses verify-email-identity`, OR
  production access is requested from AWS (24–48hr).
- The cookie signing secret lives in SSM SecureString at
  `/photo-management/cookie-secret`. Rotating it invalidates every active
  session — useful as a "sign out everywhere" lever.

**UI.** The console needs:
- Bulk upload dropzone (drag a folder, shows progress)
- Album management (create, rename, set title/cover photo)
- Per-photo tagging (checkbox grid: "which albums is this in?")
- Share link generation (pick album → button → copy link)
- Cost estimator widget

A single-page React app bundled and served from S3/CloudFront, talking to the same
Lambda API, is the cleanest split. If you want to avoid a separate build step,
server-rendered HTMX is surprisingly good for admin tools and matches your current
Python-only stack better.

Desktop-only — no need to spend effort on mobile layouts or touch UX for the console.

## 8. Upload path

Browser → pre-signed S3 PUT URLs directly (don't route bytes through Lambda, you'll
hit payload limits and burn money). Lambda's role: issue the pre-signed URLs, and on
S3 `ObjectCreated` event (via EventBridge or S3 notification → Lambda), generate
derivatives and write the Photo record.

"Automatically add to album" = admin UI passes an `album_id` as S3 object metadata
when requesting the pre-signed URL; the post-processing Lambda reads it and tags.

**Batch upload toast (Story 4).** The toast fires *after* every Photo record
has been written, not at S3-PUT time — at PUT time the derivatives Lambda hasn't
run yet and the Photo row doesn't exist. Simplest mechanism: the browser holds
the list of `photo_id`s it requested pre-signed URLs for, then polls
`GET /photos?ids=...` every ~1s until every id resolves. Show the toast when the
full set resolves (or show a partial toast with a count if some don't resolve
within ~30s and let the admin proceed anyway).

Polling beats SSE/WebSockets here — simpler, Lambda-friendly, and the poll volume
is trivial at one-admin-one-batch-at-a-time scale.

## 9. Cost estimator

PROJECT.md asks for a "rough estimate based on us-west-2 pricing." Thoughts:
- Hard-code the small number of SKUs that matter: **S3 storage ($/GB-month), S3
  requests, CloudFront egress ($/GB), Lambda (~free at your scale), DynamoDB on-demand
  (~free at your scale)**
- Query the current state: bytes stored, photos count, last-30-day egress (from
  CloudFront logs or a rough estimate from download_count × avg size)
- Render: "This month: ~$X.XX. Breakdown: storage $Y, egress $Z, …"
- **Don't** try to use the AWS Pricing API — it's a rabbit hole. Hard-code the rates
  in a `pricing.py` and update when you notice them drift. Accuracy to the dollar
  doesn't matter; order-of-magnitude does.

## 10. Open questions / things worth deciding early

- **Album ordering.** EXIF capture time, ascending (oldest first). Extract
  `DateTimeOriginal` from EXIF on upload and store on the Photo record for sort keys.
  Fall back to upload time if EXIF is missing.
- **Deletion semantics.** Hard delete (S3 objects + DynamoDB records removed
  immediately). S3 versioning provides a safety net for the rare accidental delete.
- **Share link expiry.** Permanent — share links never expire.
- **Photo privacy.** No per-photo authorization. If you have the album's share link,
  every photo in that album is viewable. No viewer auth for v1.
- **Backups.** Originals are your memories — protect against accidental deletes.
  Three tiers to consider:
  - **Versioning only (same region):** Free to enable, ~$0 extra for photos (immutable,
    rarely overwritten). Protects against accidental deletes/overwrites. Non-current
    versions persist until a lifecycle rule expires them.
  - **Cross-region replication to Glacier Deep Archive:** ~$0.00099/GB-month in the
    destination region (~$0.10/month at 100 GB). Plus ~$0.02/GB one-time transfer per
    upload. Slow/expensive retrieval, but as a disaster-recovery copy you never expect
    to touch, it's ~20× cheaper than Standard replication.
  - **Cross-region replication to Standard:** Doubles your storage bill (~$0.023/GB-month
    in destination, so ~$2.30/month at 100 GB). Plus ~$0.02/GB transfer on upload.
    Full-speed access in a second region.
  **Recommendation:** Versioning + lifecycle (expire non-current after 30d) is the
  no-brainer baseline. Add CRR to Glacier Deep Archive if you want regional redundancy
  cheaply. Standard CRR is hard to justify at this scale.

## 11. Suggested first milestone

To avoid boiling the ocean, a MVP-sized slice:

1. CDK stack, Lambda + FastAPI skeleton, `photos.jamestrachy.com` live
2. S3 bucket + derivatives Lambda
3. Basic-auth admin endpoints: upload (pre-signed), list albums, create album, tag
   photo into album
4. Public viewer: one template, renders an album by ID
5. Manual share-link generation (just hand-paste `photos.jamestrachy.com/a/<album_id>`)
6. *Then* circle back to add url-shortener integration, download tracking, and cost
   estimator

That ordering gets you functionally replacing Amazon Photos fast; the
tracking/estimator features are polish on top.

## 12. Future phases

Features parked for later — schema space is reserved where relevant, otherwise
these are explicit non-goals for v1.

- **Likes / hearts on photos.** Visible to public viewers, aggregate counter
  shown in admin views. Open questions when this lands: pure counter vs.
  per-user like records, anonymous-visitor dedupe (cookie, IP, signed session),
  un-like semantics. Not in the v1 data model — deferred until there's a
  clearer auth model for public visitors.
- **Multi-tenant / sellable-product mode.** A permissions layer beyond the
  single admin. When it lands, storage paths may need a tenant prefix,
  view-exclusion logic has to key off tenant, and the Admin role becomes one
  of several. Explicitly out of scope per §7 (YAGNI).
- **Post-production workflow.** From `PROJECT.md` "Future plans" section —
  explicitly ignored for v1.
- **Public-viewer user stories.** Story 7 ("As an unauthenticated user I can
  click the generated share link and view the gallery") has since been written
  in `PROJECT.md` and is implemented — full-bleed photo viewer with prev/next,
  responsive size, tracked download. Additional public-viewer stories (likes,
  comments, anonymous-visitor identity) remain unspecified and parked.
