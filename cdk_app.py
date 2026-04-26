#!/usr/bin/env python3
import os

import aws_cdk as cdk

from photo_management_stack import PhotoManagementStack

app = cdk.App()
PhotoManagementStack(
    app,
    "PhotoManagementStack",
    env=cdk.Environment(
        account=os.environ["CDK_DEFAULT_ACCOUNT"],
        region=os.environ["CDK_DEFAULT_REGION"],
    ),
)
app.synth()
