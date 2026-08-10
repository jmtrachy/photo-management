#!/usr/bin/env python3
import os

import aws_cdk as cdk

from photo_management_stack import PhotoManagementStack

app = cdk.App()
env = cdk.Environment(
    account=os.environ["CDK_DEFAULT_ACCOUNT"],
    region=os.environ["CDK_DEFAULT_REGION"],
)
PhotoManagementStack(
    app,
    "PhotoManagementStack",
    env=env,
)
PhotoManagementStack(
    app,
    "PhotoManagementStagingStack",
    subdomain="staging-photos",
    create_email_identity=False,
    env=env,
)
app.synth()
