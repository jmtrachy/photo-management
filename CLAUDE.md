# Photo Management

Self-hosted replacement for Amazon Photos at `photos.jamestrachy.com`. Albums, shareable links, view/download tracking, admin console for upload and tagging.

**Status:** design phase — no code yet. `PROJECT.md` is the goals/user-stories document; `PROJECT_RESPONSE.md` is the design conversation (data model, AWS architecture, milestone plan). Read both before suggesting implementation.

## Sibling projects on the same platform

This app slots into the existing `jamestrachy.com` platform. Three sibling repos under `../` establish the conventions to follow — read them on demand when relevant, don't assume.

- **`../jt-com-infra`** — `PlatformStack`. Owns the Route53 hosted zone lookup and the `*.jamestrachy.com` ACM cert (us-east-1). Exports three SSM params consumer stacks read at synth time:
  - `/platform/hosted-zone-id`
  - `/platform/hosted-zone-name`
  - `/platform/wildcard-cert-arn`
  - Key files: `platform_stack.py`, `cdk_app.py`

- **`../url-shortener`** — `l.jamestrachy.com`. POST `/generate` → 6-char ID, GET `/u/{id}` → 302 with `hit_count` increment. The photo app's share-link click tracking will integrate with this (see PROJECT_RESPONSE.md §5).
  - Key files: `app.py` (FastAPI handler), `url_shortener_stack.py` (CDK), `cdk_app.py`, `Dockerfile`

- **`../qr-code`** — `qrcode.jamestrachy.com`. QR generator + scan-tracking redirect. Same shape as url-shortener, plus an S3 bucket served via a `/qrs/*` CloudFront behavior. Calls url-shortener over HTTP — example of cross-app integration.
  - Key files: `app.py`, `qr_code_stack.py`, `cdk_app.py`, `Dockerfile`

## Conventions inherited from the siblings

Match these unless there's a deliberate reason not to:

- **Runtime:** FastAPI + `Mangum(app, lifespan="off")` on Lambda
- **Packaging:** `DockerImageFunction` from a local `Dockerfile` (Python 3.12). Architecture auto-detected via `platform.machine()` — ARM64 on dev machine, x86_64 otherwise; `ecr_assets.Platform.LINUX_ARM64`/`LINUX_AMD64` paired accordingly
- **API:** `apigw.LambdaRestApi` fronted by a `cloudfront.Distribution` with `CACHING_DISABLED` + `ALL_VIEWER_EXCEPT_HOST_HEADER` origin request policy
- **DNS:** `route53.ARecord` aliasing the subdomain (`photos`) to the CloudFront distribution, using the zone resolved from SSM
- **Storage:** DynamoDB `PAY_PER_REQUEST`, `RemovalPolicy.DESTROY` on dev resources. Atomic counters via `UpdateItem ADD … :inc`
- **Logging:** structured JSON via `logger.info(json.dumps({"event": "...", ...}))` for queryable CloudWatch Logs Insights
- **Stack layout:** `cdk_app.py` instantiates a single stack defined in `<name>_stack.py` — flat, no nested constructs

## Project-specific naming

- Stack: `PhotoManagementStack`
- Subdomain: `photos.jamestrachy.com`
- CDK entrypoint: `cdk_app.py`, stack in `photo_management_stack.py`

## Commands (once scaffolded)

- `pip install -r requirements.txt`
- `npx aws-cdk deploy PhotoManagementStack`
- `npx aws-cdk destroy PhotoManagementStack`

## Workflow

Work lands via feature branches + PRs against `main`, not direct commits.
