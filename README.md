# Photo Management

Self-hosted photo management at `photos.jamestrachy.com`. See `PROJECT.md` for the
product goals and `PROJECT_RESPONSE.md` for the design discussion.

## Current scope (Story 2 — empty grid)

This first deploy stands up the foundation:
- Magic-link sign-in (email allowlist) at `https://photos.jamestrachy.com/login`
- Authenticated admin landing page at `/` titled "Your Photographs" with a grid
  that calls `/api/photos`
- Session cookie auth on `/`, `/api/photos`, and `/logout`
- DynamoDB `Photos` table, `LoginTokens` table (with TTL), and S3 photos bucket
  (all empty, ready for upload work)
- `/api/photos` returns `{"photos": [], "cursor": null}` until upload lands

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
DynamoDB tables (`Photos`, `LoginTokens`) have `RemovalPolicy.DESTROY` and will
be deleted with the stack. The SSM cookie-secret parameter is unmanaged by CDK —
delete with `aws ssm delete-parameter --name /photo-management/cookie-secret`.

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
  - `GET /` — admin landing page (auth required)
  - `GET /api/photos` — JSON photo list (auth required)
  - `GET /login` — sign-in form
  - `POST /login` — request a magic link
  - `GET /login/verify?token=...` — consume token, set session cookie
  - `GET /logout` — clear session cookie
- `index.html` — admin landing page (vanilla HTML/CSS/JS)
- `login.html` — sign-in form
- `login_sent.html` — "check your email" confirmation
- `photo_management_stack.py` — CDK stack (Lambda, API Gateway, CloudFront,
  Route53, DynamoDB Photos + LoginTokens tables, S3 photos bucket, SES domain
  identity with DKIM)
- `cdk_app.py` — CDK app entry point
- `Dockerfile` — Lambda container image (Python 3.13)
