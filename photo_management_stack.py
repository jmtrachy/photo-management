import os
import platform as _platform

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_apigateway as apigw,
    aws_certificatemanager as acm,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_dynamodb as dynamodb,
    aws_ecr_assets as ecr_assets,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_route53 as route53,
    aws_route53_targets as route53_targets,
    aws_s3 as s3,
    aws_s3_notifications as s3n,
    aws_ses as ses,
    aws_ssm as ssm,
)
from constructs import Construct

COOKIE_SECRET_SSM_PARAM = "/photo-management/cookie-secret"
SUBDOMAIN = "photos"
ROOT_DOMAIN = "jamestrachy.com"
CUSTOM_DOMAIN = f"{SUBDOMAIN}.{ROOT_DOMAIN}"
FROM_EMAIL = f"noreply@{ROOT_DOMAIN}"
ADMIN_EMAILS = "jmtrachy@gmail.com,agentjimbo@gmail.com"


class PhotoManagementStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        photos_table = dynamodb.Table(
            self,
            "PhotosTable",
            partition_key=dynamodb.Attribute(
                name="photo_id", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )

        photos_table.add_global_secondary_index(
            index_name="ByTakenAt",
            partition_key=dynamodb.Attribute(
                name="entity_type", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="taken_at", type=dynamodb.AttributeType.NUMBER
            ),
        )

        albums_table = dynamodb.Table(
            self,
            "AlbumsTable",
            partition_key=dynamodb.Attribute(
                name="album_id", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )

        albums_table.add_global_secondary_index(
            index_name="ByCreatedAt",
            partition_key=dynamodb.Attribute(
                name="entity_type", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="created_at", type=dynamodb.AttributeType.NUMBER
            ),
        )

        memberships_table = dynamodb.Table(
            self,
            "MembershipsTable",
            partition_key=dynamodb.Attribute(
                name="pk", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="sk", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )

        memberships_table.add_global_secondary_index(
            index_name="ByPhoto",
            partition_key=dynamodb.Attribute(
                name="sk", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="pk", type=dynamodb.AttributeType.STRING
            ),
        )

        login_tokens_table = dynamodb.Table(
            self,
            "LoginTokensTable",
            partition_key=dynamodb.Attribute(
                name="token", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
            time_to_live_attribute="expires_at",
        )

        photos_bucket = s3.Bucket(
            self,
            "PhotosBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            versioned=True,
            cors=[
                s3.CorsRule(
                    allowed_methods=[
                        s3.HttpMethods.PUT,
                        s3.HttpMethods.GET,
                        s3.HttpMethods.HEAD,
                    ],
                    allowed_origins=[f"https://{CUSTOM_DOMAIN}"],
                    allowed_headers=["*"],
                    exposed_headers=["ETag"],
                    max_age=3000,
                ),
            ],
            lifecycle_rules=[
                s3.LifecycleRule(
                    noncurrent_version_expiration=Duration.days(30),
                ),
            ],
            removal_policy=RemovalPolicy.RETAIN,
        )

        zone_id = ssm.StringParameter.value_for_string_parameter(
            self, "/platform/hosted-zone-id"
        )
        zone_name = ssm.StringParameter.value_for_string_parameter(
            self, "/platform/hosted-zone-name"
        )
        cert_arn = ssm.StringParameter.value_for_string_parameter(
            self, "/platform/wildcard-cert-arn"
        )

        zone = route53.PublicHostedZone.from_public_hosted_zone_attributes(
            self,
            "Zone",
            hosted_zone_id=zone_id,
            zone_name=zone_name,
        )

        email_identity = ses.EmailIdentity(
            self,
            "EmailIdentity",
            identity=ses.Identity.public_hosted_zone(zone),
        )

        docker_dir = os.path.dirname(__file__) or "."

        is_arm = _platform.machine() == "arm64"
        arch = _lambda.Architecture.ARM_64 if is_arm else _lambda.Architecture.X86_64
        docker_platform = (
            ecr_assets.Platform.LINUX_ARM64 if is_arm else ecr_assets.Platform.LINUX_AMD64
        )

        fn = _lambda.DockerImageFunction(
            self,
            "PhotoManagementFunction",
            code=_lambda.DockerImageCode.from_image_asset(
                docker_dir, platform=docker_platform
            ),
            architecture=arch,
            memory_size=512,
            timeout=Duration.seconds(30),
            environment={
                "COOKIE_SECRET_SSM_PARAM": COOKIE_SECRET_SSM_PARAM,
                "LOGIN_TOKENS_TABLE": login_tokens_table.table_name,
                "PHOTOS_TABLE": photos_table.table_name,
                "ALBUMS_TABLE": albums_table.table_name,
                "MEMBERSHIPS_TABLE": memberships_table.table_name,
                "PHOTOS_BUCKET": photos_bucket.bucket_name,
                "ADMIN_EMAILS": ADMIN_EMAILS,
                "FROM_EMAIL": FROM_EMAIL,
                "BASE_URL": f"https://{CUSTOM_DOMAIN}",
            },
        )

        login_tokens_table.grant_read_write_data(fn)
        photos_table.grant_read_data(fn)
        albums_table.grant_read_write_data(fn)
        memberships_table.grant_read_write_data(fn)
        photos_bucket.grant_read_write(fn)

        derivatives_fn = _lambda.DockerImageFunction(
            self,
            "DerivativesFunction",
            code=_lambda.DockerImageCode.from_image_asset(
                docker_dir,
                file="Dockerfile.derivatives",
                platform=docker_platform,
            ),
            architecture=arch,
            memory_size=2048,
            timeout=Duration.minutes(2),
            environment={
                "PHOTOS_TABLE": photos_table.table_name,
                "PHOTOS_BUCKET": photos_bucket.bucket_name,
            },
        )

        photos_table.grant_write_data(derivatives_fn)
        photos_bucket.grant_read_write(derivatives_fn)

        photos_bucket.add_event_notification(
            s3.EventType.OBJECT_CREATED,
            s3n.LambdaDestination(derivatives_fn),
            s3.NotificationKeyFilter(prefix="originals/"),
        )

        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameter"],
                resources=[
                    f"arn:aws:ssm:{self.region}:{self.account}:parameter{COOKIE_SECRET_SSM_PARAM}"
                ],
            )
        )

        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ses:SendEmail", "ses:SendRawEmail"],
                resources=[
                    f"arn:aws:ses:{self.region}:{self.account}:identity/*"
                ],
            )
        )

        api = apigw.LambdaRestApi(
            self,
            "PhotoManagementApi",
            handler=fn,
        )

        cf_cert = acm.Certificate.from_certificate_arn(self, "WildcardCert", cert_arn)

        distribution = cloudfront.Distribution(
            self,
            "Distribution",
            domain_names=[CUSTOM_DOMAIN],
            certificate=cf_cert,
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.HttpOrigin(
                    f"{api.rest_api_id}.execute-api.{self.region}.amazonaws.com",
                    origin_path=f"/{api.deployment_stage.stage_name}",
                ),
                allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
            ),
        )

        route53.ARecord(
            self,
            "AliasRecord",
            zone=zone,
            record_name=SUBDOMAIN,
            target=route53.RecordTarget.from_alias(
                route53_targets.CloudFrontTarget(distribution)
            ),
        )

        CfnOutput(self, "ApiUrl", value=api.url)
        CfnOutput(self, "CustomUrl", value=f"https://{CUSTOM_DOMAIN}")
        CfnOutput(self, "PhotosBucketName", value=photos_bucket.bucket_name)
        CfnOutput(self, "PhotosTableName", value=photos_table.table_name)
        CfnOutput(self, "AlbumsTableName", value=albums_table.table_name)
        CfnOutput(self, "MembershipsTableName", value=memberships_table.table_name)
        CfnOutput(self, "LoginTokensTableName", value=login_tokens_table.table_name)
        CfnOutput(self, "EmailIdentityName", value=email_identity.email_identity_name)
