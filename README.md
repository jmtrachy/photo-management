# Photo Management

Self-hosted photo management at `photos.jamestrachy.com`. See `PROJECT.md` for the
product goals and `PROJECT_RESPONSE.md` for the design discussion.

## Current scope

Working features, mapped to user stories in `PROJECT.md`:

- **Story 1** — admin albums grid at `/albums` with cover thumbnails and per-album view counts
- **Story 2** — recent-photos grid at `/` with date dividers and per-photo view/download counts
- **Story 3** (partial) — photo detail at `/photo/{id}` with medium image, prev/next arrow navigation across all photos by taken-at, and view/download original links. EXIF panel, album-membership list, and delete cascade not yet built.
- **Story 4** — drag-and-drop upload via presigned S3 PUT; the derivatives Lambda generates `thumb`/`medium` images, extracts EXIF (taken-at), and writes the Photo record. "Add to album" modal tags the batch into an existing album.
- **Story 6** — `POST /api/albums/{id}/shares` issues an 8-char slug at `/a/<slug>`; per-album share list shown in the admin album view and copyable.
- **Story 7** — unauthenticated viewer at `/a/{share_id}` (album grid) and `/a/{share_id}/{photo_id}` (full-bleed photo with translucent prev/next arrows, responsive image size — original for viewport ≥ 1024px, medium below — and a tracked "Download Full Res image" link). Album view increments `Album.view_count`; per-photo view increments `Photo.view_count`; downloads increment `Photo.download_count`.

Not yet built: Story 3's EXIF/album-list/delete pieces, Story 5 (tagging), the cost-estimator widget (PROJECT.md §"Management Console").

Supporting infrastructure:
- Magic-link sign-in (email allowlist) via SES; session cookies signed with `itsdangerous` (30-day max-age, HttpOnly + Secure + SameSite=Lax)
- DynamoDB tables: `Photos`, `Albums`, `Memberships`, `Shares`, `LoginTokens` (all `PAY_PER_REQUEST`, `DESTROY` removal policy)
- S3 photos bucket: `originals/<photo_id>.<ext>` (private, presigned access) and `derivatives/<photo_id>/{thumb,medium}.jpg`
- Two Lambdas in one stack: the FastAPI/Mangum API and a derivatives processor triggered by S3 `ObjectCreated` on `originals/`
- Error-logging middleware in `app.py` emits a single JSON CloudWatch record per unhandled exception (event, method, path, exception type/message, full traceback)

## Prerequisites

- Python 3.12+ (for local CDK)
- Docker (running, for Lambda image build)
- Node.js (for CDK CLI)
- AWS CLI configured for the target account
- `jt-com-infra/PlatformStack` already deployed (provides the SSM params for the
  hosted zone and wildcard cert)

## One-time setup

### 1. Install dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Set the cookie signing secret

The Lambda signs session cookies with a secret stored at SSM Parameter Store path
`/photo-management/cookie-secret` (SecureString). Generate and store one:

```bash
aws ssm put-parameter \
  --name /photo-management/cookie-secret \
  --type SecureString \
  --value "$(openssl rand -hex 32)"
```

To force everyone to sign in again, re-run with `--overwrite` and a new value.
The Lambda caches the value per container; rotation takes effect on the next
cold start.

### 3. SES setup

The stack creates an SES domain identity for `jamestrachy.com` with auto-DKIM
records in Route 53. After the first `cdk deploy`, DKIM propagation typically
takes 5–15 minutes. Check status:

```bash
aws ses get-identity-verification-attributes \
  --identities jamestrachy.com \
  --region <your-region>
```

Wait until `VerificationStatus` is `Success` before testing sign-in.

**Sandbox mode (new accounts).** New AWS accounts start in SES sandbox: you can
only send to *verified* recipient addresses. Verify each admin email once:

```bash
aws ses verify-email-identity \
  --email-address jmtrachy@gmail.com \
  --region <your-region>

aws ses verify-email-identity \
  --email-address agentjimbo@gmail.com \
  --region <your-region>
```

Each address receives an AWS verification email — click the link to confirm.
Alternatively, request SES production access (24–48 hour review) at
https://console.aws.amazon.com/ses/home#/account to skip recipient verification.

## Deploy

```bash
npx aws-cdk deploy PhotoManagementStack
```

Outputs include `CustomUrl`, the bucket name, and the table names.

## Tear down

```bash
npx aws-cdk destroy PhotoManagementStack
```

The S3 bucket has `RemovalPolicy.RETAIN` to protect uploaded originals — destroy
will leave it behind. Empty and delete it manually if you really want it gone.
DynamoDB tables (`Photos`, `Albums`, `Memberships`, `Shares`, `LoginTokens`) have
`RemovalPolicy.DESTROY` and will be deleted with the stack. The SSM cookie-secret
parameter is unmanaged by CDK — delete with
`aws ssm delete-parameter --name /photo-management/cookie-secret`.

## Access

1. Open `https://photos.jamestrachy.com/` in a browser.
2. You're redirected to `/login`. Enter `jmtrachy@gmail.com` (or
   `agentjimbo@gmail.com`) and submit.
3. Check the inbox for the sign-in email and click the link. It's good for 15
   minutes and works exactly once.
4. The "Your Photographs" page loads with an empty grid ("No photos yet."). The
   session cookie persists for 30 days; sign out via the link in the header.

To change the admin allowlist, edit `ADMIN_EMAILS` in `photo_management_stack.py`
and redeploy.

## Project layout

- `app.py` — FastAPI app (Lambda handler via Mangum). Routes:
  - `GET /`, `/albums`, `/album/{id}`, `/photo/{id}` — admin HTML pages (auth required)
  - `GET /api/photos`, `/api/photos/{id}` — photo list and detail
  - `POST /api/photos/exists` — derivatives-Lambda completion poll for batch upload
  - `GET /api/albums`, `/api/albums/{id}` — album list and detail
  - `POST /api/albums` — create album
  - `POST /api/albums/{id}/photos` — tag photos into an album
  - `POST /api/uploads/presign` — issue presigned S3 PUT URLs
  - `POST /api/albums/{id}/shares`, `GET /api/albums/{id}/shares` — create / list share links
  - `GET /a/{share_id}` — public album viewer (no auth)
  - `GET /a/{share_id}/{photo_id}` — public photo viewer (no auth)
  - `GET /api/public/shares/{share_id}` — JSON for the public album viewer
  - `GET /api/public/shares/{share_id}/photos/{photo_id}` — JSON for the public photo viewer
  - `GET /api/public/shares/{share_id}/photos/{photo_id}/download` — increments `download_count`, 302s to a presigned URL with `Content-Disposition: attachment`
  - `GET /login`, `POST /login`, `GET /login/verify?token=...`, `GET /logout` — auth
- `derivatives.py` — separate Lambda triggered by S3 `ObjectCreated` on `originals/`. Generates `derivatives/{photo_id}/thumb.jpg` and `medium.jpg`, extracts EXIF, writes the Photo record to DynamoDB.
- `index.html`, `albums.html`, `album.html`, `photo.html` — admin HTML pages (vanilla HTML/CSS/JS)
- `public_album.html`, `public_photo.html` — unauthenticated public viewers
- `login.html`, `login_sent.html` — auth pages
- `uploads.js` — shared upload helper (drag-drop + presign + post-upload polling) used by the admin pages
- `photo_management_stack.py` — CDK stack: two Lambdas (API + derivatives), API Gateway, CloudFront, Route53 alias, the five DynamoDB tables, S3 photos bucket, SES domain identity with DKIM
- `cdk_app.py` — CDK app entry point
- `Dockerfile`, `Dockerfile.derivatives` — Lambda container images (Python 3.13)
