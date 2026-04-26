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
* Each thumbnail displays the image, the taken-date (`YYYY-MM-DD HH:mm`), view count, and download count
* Clicking a thumbnail opens the photo detail view (Story 3)
* Photos load a page at a time with infinite scroll
* Group the grid by taken-date, with a dated divider between groups
* View and download counts reflect public traffic only — admin sessions don't increment them

### 3 - As an Admin I can view the details of a particular photo
Clicking a photo opens its detail view, which shows:
* The image (medium size by default, with an option to view or download the original)
* View count and download count
* The names of all albums this photo belongs to
* EXIF details: taken date, camera type, ISO, aperture, shutter speed
* A trashcan to delete the photo. Deleting cascades:
  - Removes the Photo record
  - Removes every membership record for this photo (so it disappears from every album it was tagged into)
  - Deletes the S3 objects (thumbnail, medium, original). S3 versioning retains the original for recovery — see `PROJECT_RESPONSE.md` §10

### 4 - As an Admin I can upload photos from the Photos screen
I can drag one or more photos onto the Photos screen and they upload asynchronously:
1. The browser requests a pre-signed S3 PUT URL per photo
2. The browser PUTs each file directly to S3
3. An S3 `ObjectCreated` event triggers a derivatives Lambda, which generates the thumbnail and medium versions, extracts EXIF (taken date, camera type, ISO, aperture, shutter speed), and writes the Photo record to DynamoDB
4. Once all Photo records exist, a **single** toast appears: `"N photo(s) uploaded. Add to album?"`
5. Clicking "Add to album" opens a modal listing albums (cover thumbnail + title). Typing filters the list via a server-side substring search on album name. Selecting an album tags all N just-uploaded photos into that album.

Notes:
* One toast per batch, not one per photo
* The toast fires on Lambda completion (step 3), not on the S3 PUT (step 2), because the Photo records don't exist yet at step 2 — see `PROJECT_RESPONSE.md` §8 for the completion mechanism

