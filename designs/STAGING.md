# Staging environment
I would like to have a staging environment I can use to for both manual testing and to run automated tests against. The 
order of deployments would be:

1. Deploy next release to staging
2. Test and verify
3. Deploy verified release to production

The staging environment would live at staging.photos.jamestrachy.com. Once this is live all testing of large features
will go through Staging first, and eventually I'll want to build a deployment pipeline that goes to staging,
performs automated tests, then deploys to production.

## User Stories
The following are guidelines / goals of the staging environment

### Full site access just like prod
Going to staging.photos.jamestrachy.com should provide me the same capabilities as photos.jamestrachy.com except against
the data in the staging databases. I should be able to login to staging and use the admin portal.

### Changes to Staging data do not impact production
There should never be a way for Staging to impact production data.

### Ability to deploy to Staging
Via an NX target I want the ability to deploy to staging entirely separately from deploying
to production. Perhaps it can be as simple as an argument to NX with a target env.

## Ticketed items (in order of execution)

### 1. Parameterize PhotoManagementStack for multiple environments
Refactor `photo_management_stack.py` so `PhotoManagementStack.__init__` takes environment-specific values
(subdomain, resource name prefix, whether to create the shared SES `EmailIdentity`) instead of hardcoded
module constants. `cdk_app.py` still only instantiates the existing prod stack, passing today's values —
this PR should produce a zero-diff `cdk diff` against the deployed `PhotoManagementStack`. Pure refactor,
no infrastructure changes yet.

### 2. Stand up the staging environment
Add a second stack instantiation in `cdk_app.py` — `PhotoManagementStagingStack` — using the parameters
from #1 to deploy a fully independent copy of every resource: S3 bucket, all seven DynamoDB tables, both
Lambda functions (app + derivatives, including the S3 `originals/` event notification), API Gateway,
CloudFront distribution, and Route53 record, at `staging-photos.jamestrachy.com` (single-label subdomain,
already covered by the existing `*.jamestrachy.com` wildcard cert — no jt-com-infra changes needed).
Staging and prod share nothing except the wildcard cert, the Route53 hosted zone, and the cookie-secret
SSM parameter (safe to share — cookies are host-only scoped, so a session on one subdomain is never sent
to the other). The SES `EmailIdentity` is created by prod only; staging's Lambda just gets the same
`ses:SendEmail`/`ses:SendRawEmail` IAM grant against the already-verified domain. Deployed via
`npx aws-cdk deploy PhotoManagementStagingStack`, independent of `npx aws-cdk deploy PhotoManagementStack`.
Starts out empty — no copy-down; data gets built up directly through the staging admin UI, including
uploads and Fully Delete, which both work identically to prod since staging's data is completely isolated.

### 3. Document the staging workflow
Update CLAUDE.md's Commands section with the staging deploy/destroy commands, and update this file to
reflect the final state once staging is live.
