"""Optional API Gateway HTTP API + Lambda skeleton (disabled by default).

Why default-off
---------------
Existing RDS ``trade-recon-postgres`` is publicly accessible but IP-restricted.
Lambda outside a VPC has ephemeral IPs; opening ``0.0.0.0/0`` on the RDS SG is
unsafe. Putting Lambda in a VPC without a NAT Gateway blocks Bedrock/S3 egress.
NAT Gateway is expensive for a portfolio project — deferred to Phase 2.

When ``enableApi=true``, this stack still deploys a **stub** Lambda (health JSON)
plus HTTP API and a Secrets Manager *placeholder* for DATABASE_URL — not a second
RDS, and not a full Mangum packaging of FastAPI (that needs container/VPC design).
"""

from __future__ import annotations

from pathlib import Path

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    SecretValue,
    Stack,
    Tags,
)
from aws_cdk import aws_apigatewayv2 as apigwv2
from aws_cdk import aws_apigatewayv2_integrations as apigwv2_integrations
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

STUB_DIR = Path(__file__).resolve().parents[1] / "lambda_stubs"


class ApiStack(Stack):
    """API Gateway + Lambda stub + Secrets Manager placeholder."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        project_tag: str = "trade-recon",
        existing_rds_identifier: str = "trade-recon-postgres",
        existing_market_data_bucket: str = "",
        cors_allow_origins: list[str] | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        Tags.of(self).add("Project", project_tag)

        # Placeholder secret — set the real DATABASE_URL in console/CLI after deploy.
        # Do not put passwords in CDK source.
        db_secret = secretsmanager.Secret(
            self,
            "DatabaseUrlSecret",
            secret_name=f"{project_tag}/database-url",
            description=(
                "Placeholder: set key database_url to the SQLAlchemy URL via CLI/console. "
                "Never put the real password in CDK source."
            ),
            secret_string_value=SecretValue.unsafe_plain_text(
                '{"database_url":"REPLACE_ME"}'
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )

        api_fn = lambda_.Function(
            self,
            "ApiStubFunction",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="api_stub.handler",
            code=lambda_.Code.from_asset(str(STUB_DIR)),
            timeout=Duration.seconds(10),
            memory_size=128,
            description="Trade-recon API stub (Phase 2: Mangum + FastAPI in VPC)",
            environment={
                "PROJECT": project_tag,
                "EXISTING_RDS_IDENTIFIER": existing_rds_identifier,
                "DATABASE_URL_SECRET_ARN": db_secret.secret_arn,
                "CORS_ALLOW_ORIGINS": ",".join(cors_allow_origins or ["*"]),
            },
        )
        db_secret.grant_read(api_fn)

        if existing_market_data_bucket:
            api_fn.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["s3:GetObject", "s3:ListBucket"],
                    resources=[
                        f"arn:aws:s3:::{existing_market_data_bucket}",
                        f"arn:aws:s3:::{existing_market_data_bucket}/*",
                    ],
                )
            )

        http_api = apigwv2.HttpApi(
            self,
            "HttpApi",
            api_name=f"{project_tag}-api",
            description="Trade recon HTTP API (stub until VPC Phase 2)",
            cors_preflight=apigwv2.CorsPreflightOptions(
                allow_headers=["*"],
                allow_methods=[
                    apigwv2.CorsHttpMethod.GET,
                    apigwv2.CorsHttpMethod.POST,
                    apigwv2.CorsHttpMethod.OPTIONS,
                ],
                allow_origins=cors_allow_origins or ["*"],
                max_age=Duration.hours(1),
            ),
        )
        integration = apigwv2_integrations.HttpLambdaIntegration(
            "ApiIntegration",
            api_fn,
        )
        http_api.add_routes(
            path="/{proxy+}",
            methods=[apigwv2.HttpMethod.ANY],
            integration=integration,
        )
        http_api.add_routes(
            path="/",
            methods=[apigwv2.HttpMethod.ANY],
            integration=integration,
        )

        self.http_api = http_api
        self.api_function = api_fn
        self.db_secret = db_secret

        CfnOutput(self, "HttpApiUrl", value=http_api.api_endpoint or "")
        CfnOutput(self, "DatabaseUrlSecretArn", value=db_secret.secret_arn)
        CfnOutput(self, "DatabaseUrlSecretName", value=db_secret.secret_name)
        CfnOutput(
            self,
            "ApiNetworkingNote",
            value=(
                "Stub only. Do not open RDS SG to 0.0.0.0/0. "
                "Phase 2: Lambda in VPC (private subnets) to reach RDS; "
                "avoid NAT or use VPC endpoints for Bedrock/S3."
            ),
        )
