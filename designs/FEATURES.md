# Features

Ideas and specs for future work — not scheduled, not being worked on yet. Pull an item out into its own
branch/doc (like STAGING.md, MULTITENANT.md) when it's actually time to build it.

## Deactivate share links

Right now a share link is either live or fully deleted — there's no in-between. I'd like the ability to
**deactivate** an album share so its public link stops working, without losing the share record (view/download
counts, `created_at`, the slug itself) in case I want to reactivate it later. "Deactivate"/"reactivate" is the
working language, open to something cleverer.

### Behavior

- A deactivated share's public pages (`GET /a/{share_id}`, `GET /a/{share_id}/{photo_id}`, and the JSON
  endpoints they call — `app.py:1401`, `app.py:1726`, `get_public_album`) stop rendering the album. Instead
  they show a small "this content is no longer shared" page — working copy: *"This content is no longer
  shared. Please reach out to @jmtrachy on Instagram with any concerns."* (ties naturally to the IG link
  already added to the public pages).
- The admin side is unaffected — `list_album_shares` (`app.py:1353-1368`) and the share-links list in the
  album detail sidebar (`album.html:1778`, `renderShares`) should still show deactivated shares, just visually
  marked (e.g. greyed out / a "Deactivated" badge), with a toggle to reactivate.
- Reactivating restores the exact same `share_id`/URL — nothing is reminted. Existing links people already
  have (e.g. saved, texted, bookmarked) work again immediately once reactivated.
- View/download counts on a deactivated share should freeze, not reset — deactivating is not the same as
  `reset_album_counts` (`app.py:787-799`).
- Collection shares (`entity_type == "collection"`, `_is_collection_share`) presumably want the same
  treatment eventually, but scope the first pass to album shares only, matching how this was asked.

### Likely shape

- `database/shares.py`: a `status` attribute on the share record (`"active"` default via
  `create_album_share`, `app.py`/`database/shares.py:50-71`), plus `deactivate_share(share_id)` /
  `reactivate_share(share_id)` setters — same pattern as the existing `mark_zip_pending`/`mark_zip_ready`/
  `mark_zip_failed` tri-state field on the same table.
- New admin endpoint(s): `PUT /api/albums/{album_id}/shares/{share_id}/deactivate` and `.../reactivate` (or a
  single `PUT .../status`), gated by `require_admin` like every other album-mutation endpoint.
- The three public-facing read paths (`public_album_page`, `public_photo` page, `get_public_album`) each
  already start with `share = await shares.get_share(share_id)` — add one shared check right after that
  (`if share.get("status") == "deactivated": return <the "no longer shared" response>`) rather than
  duplicating the check three different ways.
- The "no longer shared" response needs distinct handling for the two response types already in play here:
  `public_album_page`/photo page return `HTMLResponse` (a small static-ish page, could reuse the
  `_PUBLIC_ALBUM_HTML` shell or be its own minimal template), while `get_public_album` is a JSON API called by
  that page's own JS — it should probably 404 or return a distinct status the frontend checks for, so
  `public_album.html`'s JS can render the same messaging instead of a generic "Failed to load album" error.
