# Stats / view analytics

Right now view/download activity only exists as two things: a single atomic running total per album/photo
(`view_count`/`download_count`, `database/albums.py:133-148`, `database/photos.py:75-95`) and whatever's in
CloudWatch, which isn't queryable for trends. I'd like to answer three things:

1. What changed across all albums since yesterday — ideally an emailed morning report.
2. Which albums were hottest over the last 30 days.
3. The same questions for individual photos eventually, treated separately because the entity count is much
   larger (73 albums today after one soccer season; photo count will always be a multiple of that).

## Data model: daily counters, keyed date-first

Add a second increment at the same call sites as the existing `increment_view_count`/`increment_download_count`
functions, into a new table/GSI storing one item per (entity, date) with a view/download count, incremented
via `ADD ... :one` just like the existing counters.

The key design choice is **which field leads the key** — this was the point raised and resolved in
conversation before writing this doc:

- An entity-first key (`pk=ALBUM#{id}, sk=DATE#{date}`) is right for **charting one album's history over
  time** — a single-partition query, O(1) regardless of total album count. Good for a per-album detail-page
  graph.
- But use it for the two cross-album reports above and every query becomes "fetch this album's bucket" ×
  every album — O(albums), which is exactly the wrong axis to scale on given multi-tenancy is on the roadmap
  (album count only grows, forever, across every tenant).

Fix: also key **date-first** — `pk=DATE#{date}, sk=ALBUM#{album_id}` — either as the base table or as a GSI
on top of the entity-first base table (DynamoDB maintains a GSI automatically from the same write; no
dual-write app logic needed). This changes the two report queries from O(albums) to O(days-in-window):

- **Report #1 (delta since yesterday, all albums)**: one `Query` on `pk=DATE#{yesterday}` returns every
  album's row for that day in a single call. Cost doesn't grow with album count.
- **Report #2 (hottest in last 30 days)**: 30 queries, one per day in the window, each returning that day's
  active albums; sum and sort in the report Lambda. Cost is bounded by the window length (30), not by album
  or tenant count — the number that actually needs to stay flat as things scale.

Items only get written on days an entity actually had a view/download, so the date-first partitions stay
proportional to real activity, not to catalog size.

## Photos (#3)

Applying the exact same date-first design (`pk=DATE#{date}, sk=PHOTO#{photo_id}`) holds up for photos too,
despite the much larger entity count — the 30-day report still costs 30 queries, each bounded by how many
distinct photos were actually viewed that day, not by total photos in the library. So this doesn't need to
wait on [[POSTGRES.md]] the way I originally thought when we discussed it: DynamoDB was ruled out too quickly
before working through the date-first fix. Where a relational store would still genuinely win is *ad hoc*
questions this design doesn't pre-plan for — "compare this September to last," "top photo per album," any
slicing not baked into the GSI shape — DynamoDB requires the access pattern decided up front; SQL doesn't.
None of the three reports above need that flexibility yet.

## Multi-tenancy note

If/when tenancy lands, scoping the date-first partition key per-tenant (e.g. `pk=TENANT#{tid}#DATE#{date}`)
keeps each tenant's report query at O(days) rather than O(that tenant's album count) — consistent with the
GSI-redesign approach already scoped in [[MULTITENANT.md]].

## Open pieces, not yet designed

- The nightly report job itself: an EventBridge-scheduled Lambda, reusing the SES identity already wired up
  for magic-link login to send the email.
- Exact table vs. GSI choice (new dedicated table vs. GSI on an existing one) and item shape/attribute names.
- Whether photo-level daily counters get built now alongside albums, or deferred until there's an actual
  screen/report that wants them (album-level solves all three current use cases except the photo half of
  #3).
