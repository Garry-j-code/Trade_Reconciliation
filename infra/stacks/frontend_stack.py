"""S3 static site + CloudFront for the React dashboard (default deployable stack)."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from aws_cdk import (
    CfnOutput,
    Duration,
    Fn,
    RemovalPolicy,
    Stack,
    Tags,
)
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3deploy
from constructs import Construct

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"


class FrontendStack(Stack):
    """Cheap static hosting. Does not touch the market-data bucket."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        project_tag: str = "trade-recon",
        deploy_assets: bool = True,
        api_origin_domain: Optional[str] = None,
        cognito_user_pool_id: Optional[str] = None,
        cognito_client_id: Optional[str] = None,
        cognito_region: str = "us-east-1",
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        Tags.of(self).add("Project", project_tag)

        site_bucket = s3.Bucket(
            self,
            "FrontendBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            versioned=False,
            # Portfolio project: destroy empty bucket on stack delete.
            # Never points at the market-data bucket.
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        error_responses = [
            cloudfront.ErrorResponse(
                http_status=403,
                response_http_status=200,
                response_page_path="/index.html",
                ttl=Duration.minutes(5),
            ),
        ]
        additional_behaviors: dict[str, cloudfront.BehaviorOptions] = {}
        if api_origin_domain:
            api_origin = origins.HttpOrigin(
                api_origin_domain,
                protocol_policy=cloudfront.OriginProtocolPolicy.HTTP_ONLY,
                http_port=80,
                read_timeout=Duration.seconds(60),
                keepalive_timeout=Duration.seconds(5),
            )
            api_behavior = cloudfront.BehaviorOptions(
                origin=api_origin,
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                origin_request_policy=(
                    cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER
                ),
                compress=True,
            )
            additional_behaviors = {
                "/api/*": api_behavior,
                "/health": api_behavior,
            }
        else:
            error_responses.append(
                cloudfront.ErrorResponse(
                    http_status=404,
                    response_http_status=200,
                    response_page_path="/index.html",
                    ttl=Duration.minutes(5),
                )
            )

        distribution = cloudfront.Distribution(
            self,
            "FrontendDistribution",
            default_root_object="index.html",
            comment=f"{project_tag} dashboard",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(site_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD_OPTIONS,
                cached_methods=cloudfront.CachedMethods.CACHE_GET_HEAD_OPTIONS,
                compress=True,
            ),
            additional_behaviors=additional_behaviors or None,
            error_responses=error_responses,
        )

        if deploy_assets:
            if not FRONTEND_DIST.is_dir() or not (FRONTEND_DIST / "index.html").is_file():
                raise FileNotFoundError(
                    f"Missing frontend build at {FRONTEND_DIST}. "
                    "Run: cd frontend && npm ci && npm run build"
                )
            sources = [s3deploy.Source.asset(str(FRONTEND_DIST))]
            if cognito_user_pool_id and cognito_client_id:
                sources.append(
                    s3deploy.Source.data(
                        "config.json",
                        Fn.join(
                            "",
                            [
                                '{"cognitoUserPoolId":"',
                                cognito_user_pool_id,
                                '","cognitoClientId":"',
                                cognito_client_id,
                                '","cognitoRegion":"',
                                cognito_region,
                                '","authDisabled":false}',
                            ],
                        ),
                    )
                )
            s3deploy.BucketDeployment(
                self,
                "DeployFrontend",
                sources=sources,
                destination_bucket=site_bucket,
                distribution=distribution,
                distribution_paths=["/*"],
                memory_limit=256,
            )

        self.site_bucket = site_bucket
        self.distribution = distribution

        CfnOutput(self, "FrontendBucketName", value=site_bucket.bucket_name)
        CfnOutput(
            self,
            "CloudFrontDomainName",
            value=distribution.distribution_domain_name,
        )
        CfnOutput(
            self,
            "CloudFrontURL",
            value=f"https://{distribution.distribution_domain_name}",
        )
        CfnOutput(
            self,
            "CloudFrontDistributionId",
            value=distribution.distribution_id,
        )
