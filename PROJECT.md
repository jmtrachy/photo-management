# Photo Management App - Context

I've been using Amazon Photos for a while as a means for storing and sharing photos with other folks.
It offers a few features I like, but mostly is not great. These are my thoughts for creating my own
app as a replacement for Amazon Photos that allows more flexibility in features.

## Current features Amazon Photos grants
* Free photo storage (an obvious perk)
* Ability to generate shareable links
* Ability to put photos into 0..n albums for organization (and sharing)
* A UI for uploading photos for admins (such as myself)
* A pretty crappy but available UI for folks to browse who are not amazon photo members

I would like to emulate almost all of these features (except the "crappy" part of the UI)

## Additional features for photos.jamestrachy.com
* Tracking clicks into a shared link (using the ../url-shortener app at l.jamestrachy.com)
* Brand the site as my own (photos.jamestrachy.com)
* Track downloads of individual images (mechanism TBD)
* Allow titles for shared albums
* A Management Console that allows for:
  * Easily uploading batches of photos that are automatically added to an album
  * Ability to easily tag photos into multiple albums
  * Ability to create shareable links (using ../url-shortener) to albums that allow for photos to be added after the link has been created
  * Provides a cost estimate for the app based on standard us-west-2 pricing of rough estimates of S3 and other AWS resources
  * Requires login of some sort

## Future plans for photo management
One of the more difficult aspects of photography is the post-production process. My personal expertise is in soccer photos which means
during the course of a game I might easily take 1000 photos, 900 at least of which are crap. After deleting all the crap photos
I end up needing to crop, adjust, label, and upload 100 photos into an album. It takes me hours to do so.

I would like a better process than my current process. I don't think there's any getting around me having to do some
cropping of individual photos, or labeling them (currently I change the name of the file).

Actually lets ignore this section for now.

## User Stories

### 1 - As an Admin I can load a UI that shows all my albums
When logging into the site, the first screen I land on is a grid of all my Albums. Each card shows the album's cover thumbnail, title, and view count.
* The thumbnail is the photo previously specified as the album's cover image
* Albums load a page at a time, ordered most-recently-created first
* Infinite scroll: as the viewer nears the end of the current page, the next page loads
* View count is `album.view_count` — incremented only by public (non-admin) visits to the album's share page

### 2 - As an Admin I can view all recently added photos
A navigation element titled "Photos" shows a grid of my photos, most-recently-taken first. When a photo has no embedded timestamp, its upload time is the fallback.
1. Each thumbnail displays the image, the taken-date (`YYYY-MM-DD HH:mm`), view count, and download count
2. Clicking a thumbnail opens the photo detail view (Story 3)
3. Photos load a page at a time with infinite scroll
4. Group the grid by taken-date, with a dated divider between groups in the format `YYYY-MM-DD`
5. View and download counts reflect public traffic only — admin sessions don't increment them

### 3 - As an Admin I can view the details of a particular photo
Clicking a photo opens its detail view, which shows:
1. The image (medium size by default, with an option to view or download the original)
2. View count and download count
3. The names of all albums this photo belongs to
4. EXIF details: taken date, camera type, ISO, aperture, shutter speed
5. A trashcan to delete the photo. Deleting cascades:
  * Removes the Photo record
  * Removes every membership record for this photo (so it disappears from every album it was tagged into)
  * Deletes the S3 objects (thumbnail, medium, original). S3 versioning retains the original for recovery — see `PROJECT_RESPONSE.md` §10

### 4 - As an Admin I can upload photos from the Photos screen
I can drag one or more photos onto the Photos screen and they upload asynchronously:
1. The browser requests a pre-signed S3 PUT URL per photo
2. The browser PUTs each file directly to S3
3. An S3 `ObjectCreated` event triggers a derivatives Lambda, which generates the thumbnail and medium versions, extracts EXIF (taken date, camera type, ISO, aperture, shutter speed), and writes the Photo record to DynamoDB
4. Once all Photo records exist, a **single** toast appears: `"N photo(s) uploaded. Add to album?"`
5. Clicking "Add to album" opens a modal listing albums (cover thumbnail + title). Typing filters the list via a server-side substring search on album name. Selecting an album tags all N just-uploaded photos into that album.
6. Once a photo is successfully uploaded refresh the gallery of photos on the screen.

Notes:
* One toast per batch, not one per photo
* The toast fires on Lambda completion (step 3), not on the S3 PUT (step 2), because the Photo records don't exist yet at step 2 — see `PROJECT_RESPONSE.md` §8 for the completion mechanism

### 5 - As an Admin I can tag photos and browse by tag
Tags are informal, free-form labels distinct from albums. Each photo supports 1..n tags.
1. From the photo detail view (Story 3), I can add or remove tags. Tags are free-form text I type in.
2. A "Tags" navigation element lists every tag I've used, with a count of photos per tag. Clicking a tag opens a grid of every photo carrying that tag, ordered most-recently-taken first, with infinite scroll.
3. The same view includes an "Untagged" entry — all photos with zero tags — so I can find ones still needing categorization.
4. Tag-photo relationships live in a separate `PhotoTags` table; see `PROJECT_RESPONSE.md` §2 for the data model.

### 6 - As an admin I can create a shareable link to an album
When viewing any album a "share" icon exists which generates a shortened version of a URL to the public viewing of that album. 
1. The link is immediately put into the user's clipboard
2. The link has the structure https://photos.jamestrachy.com/a/<hashed-8-character-slug>
3. The link is stored in alongside the album in Dynamo
4. Multiple links per album can be created - every time the button is presses
5. The link is shown thereafter on the admin's view of the albums page so it can be grabbed again
6. The link provides access to non-authenticated users to the album. The album view for unauthenticated users should be similar to the view by admins, except it doesn't have any actions available, doesn't show a menu, doesn't show a Sign Out, and doesn't show the available links.

### 7 - As an unauthenticated user I can click the generated share link and view the gallery
1. Clicking on a link brings up the full resolution image on the screen, just like on the admin gallery with arrows and all

### 8 - As an Admin I can choose which photo is the album's cover
The album's cover photo is the one shown on the albums grid card (Story 1) and on the public viewer's preview chrome. By default it's auto-picked from the first batch of photos added to the album, but I can change it at any time.
1. From the admin album view, every photo card has a circular checkbox in the upper-right that appears on hover (matching the multi-select pattern on the All Photos screen, Story 4 step 5).
2. Clicking the checkbox enters selection mode: the sidebar swaps its upload controls for a select-mode bar showing the count of selected photos and a "Set as cover" button.
3. "Set as cover" is enabled only when exactly one photo is selected. Other future selection-mode actions (e.g. "Remove from album") may allow multiple selections.
4. Clicking "Set as cover" sets `album.cover_photo_id` to the selected photo, exits selection mode, and updates the visible cover indicator.
5. A subtle "★ Cover" pill is overlaid on the current cover photo's thumbnail so the admin can always tell which one it is at a glance.
6. The Cancel link in the select-mode bar (and the Esc key) exits selection mode without applying any action.

### 9 - As an Admin I want to remove photos from an album
Within the admin Album gallery page when selecting 1..n photos an option for "Remove" should be available. When removed
the photo is simply not in the album anymore, but remains on the site in general. Subsequent views of the album on
either the admin or public gallery page will not show this photo. If the photo is the cover photo then select some
other random photo to be the cover photo.

### 10 - As an Admin I can group albums into a Collection with a public page
A Collection is a named grouping of albums (e.g. "2026 U14 Girls Soccer Season"). The motivation is sports
photography: today I send each game's album as a separate link, and recipients lose them over time. A Collection
gives the team a single durable page that lists every game's album.

An album can belong to 0..n Collections (a girl might be on two teams). A Collection has a title for now —
no cover photo. Within a Collection, each album is either **listed** (shown on the public page) or **hidden**
(in the Collection for admin grouping only, e.g. per-girl albums I tag for organization but don't surface to
the whole team).

#### Public collection page
1. URL: `https://photos.jamestrachy.com/c/<8-char-slug>`. Slugs are minted the same way as album shares — alphanumeric, generated server-side, stored in the same `Shares` table with an `entity_type` of `"collection"`.
2. Renders the collection title at the top, then a grid of cards for **listed** albums only.
3. Each card shows: cover photo (grey placeholder if the album has no cover yet), album title, and creation date in small text underneath.
4. Cards order most-recently-created-album first.
5. Clicking a card navigates to that album's public viewer at `/a/<album-share-slug>` (see slug-mint rules below).
6. `Collection.view_count` increments atomically on each public page load — admin sessions don't count.

#### Slug-mint rules for cards
When an album is added to a Collection as **listed**, or promoted from hidden→listed:
- If the album already has any `Shares` row, reuse the **newest** existing one.
- Otherwise mint a fresh album-share slug and store it.
- Either way, the chosen `share_id` is persisted on the `CollectionAlbums` membership row so the card link is stable for that collection.
- Hidden albums don't trigger a mint. Demoting listed→hidden keeps the stored slug (existing album-shares are permanent — see Story 6).

#### Admin: Collections list
1. New "Collections" nav element alongside Albums and Photos.
2. Grid of all collections, ordered most-recently-created first, showing each collection's title and the shareable public link.
3. "New collection" button → modal asking for a title, then redirects to the collection detail page.

#### Admin: Collection detail page
1. Two sections stacked vertically: **Listed** and **Hidden**. Each is a grid of album cards (cover thumbnail, title, created date) — the same card shape as the public page.
2. Within each section, cards order most-recently-created-album first.
3. Each card has the same hover-revealed circular checkbox pattern as Story 4 / Story 8 / Story 9.
4. Selection mode is scoped to one section at a time. Selecting in the other section clears the prior selection and switches the active section.
5. The sidebar swaps its add-album controls for a select-mode bar whose actions depend on the active section:
  * Listed selection → "Hide" (sets membership `visibility="hidden"`) and "Remove" (removes from collection)
  * Hidden selection → "Make visible" (sets `visibility="listed"` — also runs the slug-mint rule above) and "Remove"
6. The select-mode bar shows the count of selected albums and a Cancel link. Esc also exits selection mode.
7. "Add albums" button opens a picker modal with substring search over album titles (same pattern as the Add-to-album modal in Story 4). Multi-select so a season's worth of games can be added in one pass. Newly-added albums default to **listed**.

#### Deletion
Hard delete: removes the Collection row and every `CollectionAlbums` row. Album-shares that the Collection minted remain alive — share URLs are permanent (Story 6).

Notes:
* See `PROJECT_RESPONSE.md` for the `CollectionAlbums` adjacency-table layout (PK `COLLECTION#<id>`, SK `ALBUM#<id>`, with `visibility` and `share_id` on the row) and the inverse GSI keyed on `ALBUM#<id>` that powers future "which collections is this album in" lookups.
* Security model: by-obscurity. Anyone with the `/c/<slug>` link sees every listed album in the collection. Hidden albums are reachable only via their own album-share links.
