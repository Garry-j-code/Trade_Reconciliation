#!/usr/bin/env python3
"""AWS CDK app — Trade Reconciliation (account 894831047463, us-east-1).

Default deploy (cheap / working):
  AuthStack — Cognito user pool + seeded demo analyst
  FrontendStack — S3 + CloudFront for the React dashboard
  Ec2ApiStack — t4g.micro FastAPI in the RDS VPC (enableEc2Api, default true)
  PipelineStack — weekday EventBridge → Step Functions → POST /api/recon/run

Optional (context flags):
  enablePipeline=true (default) — scheduled recon + daily HITL memory backfill
  enableApi=false (default) — API Gateway + Lambda stub (legacy scaffolding)
  enableEc2Api=true (default) — HTTP API on EC2; inbound :80 from CloudFront prefix list only
  enableAuth=true (default) — Cognito

Reuse (do not recreate):
  S3 market data: trade-recon-market-data-gagan-8948-us-east-1
  RDS: trade-recon-postgres
"""

from __future__ import annotations

import os

import aws_cdk as cdk

from stacks.api_stack import ApiStack
from stacks.auth_stack import AuthStack
from stacks.ec2_api_stack import Ec2ApiStack
from stacks.frontend_stack import FrontendStack
from stacks.pipeline_stack import PipelineStack


def _ctx_bool(app: cdk.App, key: str, default: bool) -> bool:
    raw = app.node.try_get_context(key)
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).lower() in {"1", "true", "yes", "y"}


def main() -> None:
    app = cdk.App()

    env = cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT", "894831047463"),
        region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
    )
    project_tag = str(app.node.try_get_context("projectTag") or "trade-recon")
    market_bucket = str(
        app.node.try_get_context("existingMarketDataBucket")
        or "trade-recon-market-data-gagan-8948-us-east-1"
    )
    rds_id = str(
        app.node.try_get_context("existingRdsIdentifier") or "trade-recon-postgres"
    )
    vpc_id = str(
        app.node.try_get_context("existingVpcId") or "vpc-0f92428efe0f6e0c1"
    )
    rds_sg = str(
        app.node.try_get_context("existingRdsSecurityGroupId")
        or "sg-0f0bab4863d9cfe6c"
    )
    api_subnet = str(
        app.node.try_get_context("apiSubnetId") or "subnet-03c8c10c72fed25b4"
    )
    cloudfront_origin = str(
        app.node.try_get_context("cloudfrontOrigin")
        or "https://d1a8rtzx54qkw.cloudfront.net"
    )

    enable_api = _ctx_bool(app, "enableApi", False)
    enable_ec2_api = _ctx_bool(app, "enableEc2Api", True)
    enable_pipeline = _ctx_bool(app, "enablePipeline", True)
    enable_auth = _ctx_bool(app, "enableAuth", True)
    # Skip uploading assets during synth-only / CI when dist is absent.
    deploy_frontend_assets = _ctx_bool(app, "deployFrontendAssets", True)

    auth = None
    if enable_auth:
        auth = AuthStack(
            app,
            "TradeReconAuth",
            env=env,
            project_tag=project_tag,
            description="Cognito user pool + seeded demo analyst",
        )

    ec2_api = None
    if enable_ec2_api:
        ec2_api = Ec2ApiStack(
            app,
            "TradeReconEc2Api",
            env=env,
            project_tag=project_tag,
            existing_vpc_id=vpc_id,
            existing_rds_security_group_id=rds_sg,
            api_subnet_id=api_subnet,
            existing_market_data_bucket=market_bucket,
            cloudfront_origin=cloudfront_origin,
            cognito_user_pool_id=auth.user_pool_id if auth else "",
            cognito_client_id=auth.client_id if auth else "",
            description="FastAPI on t4g.micro in RDS VPC (no NAT, no open 5432)",
        )
        if auth:
            ec2_api.add_stack_dependency(auth)

    frontend = FrontendStack(
        app,
        "TradeReconFrontend",
        env=env,
        project_tag=project_tag,
        deploy_assets=deploy_frontend_assets,
        api_origin_domain=ec2_api.api_origin_domain if ec2_api else None,
        cognito_user_pool_id=auth.user_pool_id if auth else None,
        cognito_client_id=auth.client_id if auth else None,
        cognito_region="us-east-1",
        description="Trade recon React dashboard (S3 + CloudFront)",
    )
    if auth:
        frontend.add_stack_dependency(auth)

    if enable_pipeline:
        instance_override = str(app.node.try_get_context("apiInstanceId") or "").strip()
        pipeline = PipelineStack(
            app,
            "TradeReconPipeline",
            env=env,
            project_tag=project_tag,
            api_base_url=cloudfront_origin,
            instance_id=instance_override
            or (ec2_api.instance.instance_id if ec2_api else None),
            scheduler_secret=ec2_api.scheduler_secret if ec2_api else None,
            rds_identifier=rds_id,
            sunset_days=int(os.environ.get("SUNSET_DAYS") or 30),
            sunset_date=str(
                app.node.try_get_context("sunsetDate")
                or os.environ.get("SUNSET_DATE")
                or "2026-09-13"
            ),
            description="Daily blotter SSM + HITL memory backfill + 30-day sunset pause",
        )
        if ec2_api and not instance_override:
            pipeline.add_stack_dependency(ec2_api)

    if enable_api:
        ApiStack(
            app,
            "TradeReconApi",
            env=env,
            project_tag=project_tag,
            existing_rds_identifier=rds_id,
            existing_market_data_bucket=market_bucket,
            cors_allow_origins=["*"],
            description="API Gateway + Lambda stub (RDS VPC Phase 2)",
        )

    # Reference-only outputs so operators know what we reuse (not managed by CDK).
    cdk.CfnOutput(
        frontend,
        "ExistingMarketDataBucketRef",
        value=market_bucket,
        description="Reused market-data bucket (not created/destroyed by this app)",
    )
    cdk.CfnOutput(
        frontend,
        "ExistingRdsIdentifierRef",
        value=rds_id,
        description="Reused RDS instance id (not created by this app)",
    )

    app.synth()


if __name__ == "__main__":
    main()
