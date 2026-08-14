"""FastAPI on a public-subnet t4g.micro — private RDS, no NAT, no 0.0.0.0/0 on 5432."""

from __future__ import annotations

import shutil
from pathlib import Path

from aws_cdk import CfnOutput, Fn, IgnoreMode, RemovalPolicy, Stack, Tags
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3_assets as s3_assets
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

REPO_ROOT = Path(__file__).resolve().parents[2]
BUNDLE_DIR = Path(__file__).resolve().parents[1] / "ec2_api" / ".bundle"
SSM_PARAM = "/trade-recon/database-url"
CLOUDFRONT_ORIGIN_DEFAULT = "https://d1a8rtzx54qkw.cloudfront.net"
# us-east-1 managed prefix list: com.amazonaws.global.cloudfront.origin-facing
CLOUDFRONT_ORIGIN_PREFIX_LIST = "pl-3b927c52"

def _stage_api_bundle() -> Path:
    """Copy a slim app tree for the EC2 asset (never includes .env)."""
    if BUNDLE_DIR.exists():
        shutil.rmtree(BUNDLE_DIR)
    dest_backend = BUNDLE_DIR / "backend"
    shutil.copytree(
        REPO_ROOT / "backend",
        dest_backend,
        ignore=shutil.ignore_patterns(
            "__pycache__",
            "*.pyc",
            ".pytest_cache",
            "cache",
            "generated",
            "normalized",
            "matched",
            "*.parquet",
        ),
    )
    shutil.copy2(REPO_ROOT / "pyproject.toml", BUNDLE_DIR / "pyproject.toml")
    readme = REPO_ROOT / "README.md"
    if readme.is_file():
        shutil.copy2(readme, BUNDLE_DIR / "README.md")
    dest_ec2 = BUNDLE_DIR / "infra" / "ec2_api"
    dest_ec2.mkdir(parents=True, exist_ok=True)
    for name in ("bootstrap.sh", "requirements.txt", "trade-recon-api.service"):
        shutil.copy2(
            Path(__file__).resolve().parents[1] / "ec2_api" / name,
            dest_ec2 / name,
        )
    env_marker = BUNDLE_DIR / ".env"
    if env_marker.exists():
        env_marker.unlink()
    return BUNDLE_DIR


class Ec2ApiStack(Stack):
    """t4g.micro in the RDS VPC public subnet; RDS SG allows 5432 from this API SG only (+ laptop)."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        project_tag: str = "trade-recon",
        existing_vpc_id: str,
        existing_rds_security_group_id: str,
        api_subnet_id: str,
        existing_market_data_bucket: str,
        cloudfront_origin: str = CLOUDFRONT_ORIGIN_DEFAULT,
        ssm_parameter_name: str = SSM_PARAM,
        cognito_user_pool_id: str = "",
        cognito_client_id: str = "",
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        Tags.of(self).add("Project", project_tag)

        vpc = ec2.Vpc.from_vpc_attributes(
            self,
            "RdsVpc",
            vpc_id=existing_vpc_id,
            availability_zones=[
                "us-east-1a",
                "us-east-1b",
                "us-east-1c",
                "us-east-1d",
                "us-east-1e",
                "us-east-1f",
            ],
            public_subnet_ids=[
                "subnet-03c8c10c72fed25b4",
                "subnet-0ad3a273542f2893b",
                "subnet-02e3ced060353de6c",
                "subnet-0f64a41e59dd5af08",
                "subnet-0ae5f637cfca643dc",
                "subnet-069ecc91137339fba",
            ],
        )

        api_sg = ec2.SecurityGroup(
            self,
            "ApiSecurityGroup",
            vpc=vpc,
            description="Trade-recon FastAPI EC2 (HTTP from CloudFront prefix list only)",
            allow_all_outbound=True,
        )
        api_sg.add_ingress_rule(
            ec2.Peer.prefix_list(CLOUDFRONT_ORIGIN_PREFIX_LIST),
            ec2.Port.tcp(80),
            "HTTP from CloudFront origin-facing prefix list (not 0.0.0.0/0)",
        )

        rds_sg = ec2.SecurityGroup.from_security_group_id(
            self,
            "ExistingRdsSg",
            existing_rds_security_group_id,
            mutable=True,
        )
        rds_sg.add_ingress_rule(
            api_sg,
            ec2.Port.tcp(5432),
            "Postgres from trade-recon API EC2 only (not 0.0.0.0/0)",
        )

        role = iam.Role(
            self,
            "ApiInstanceRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            description="trade-recon API instance: Bedrock, S3 cache, SSM DATABASE_URL, logs",
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "AmazonSSMManagedInstanceCore"
                ),
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "CloudWatchAgentServerPolicy"
                ),
            ],
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="BedrockNovaLite",
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=[
                    "arn:aws:bedrock:*::foundation-model/amazon.nova-lite-*",
                    "arn:aws:bedrock:*::foundation-model/amazon.titan-embed-*",
                    f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/*",
                ],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="SsmDatabaseUrl",
                actions=["ssm:GetParameter", "ssm:GetParameters"],
                resources=[
                    f"arn:aws:ssm:{self.region}:{self.account}:parameter{ssm_parameter_name}"
                ],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="SsmKmsDecrypt",
                actions=["kms:Decrypt"],
                resources=["*"],
                conditions={
                    "StringEquals": {"kms:ViaService": f"ssm.{self.region}.amazonaws.com"}
                },
            )
        )
        if existing_market_data_bucket:
            role.add_to_policy(
                iam.PolicyStatement(
                    sid="MarketDataRead",
                    actions=["s3:GetObject", "s3:ListBucket"],
                    resources=[
                        f"arn:aws:s3:::{existing_market_data_bucket}",
                        f"arn:aws:s3:::{existing_market_data_bucket}/*",
                    ],
                )
            )

        scheduler_secret = secretsmanager.Secret(
            self,
            "ReconSchedulerSecret",
            secret_name=f"{project_tag}/recon-scheduler-secret",
            description="Shared secret for EventBridge/Step Functions POST /api/recon/run",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                password_length=32,
                exclude_punctuation=True,
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )
        scheduler_secret.grant_read(role)
        logs.LogGroup(
            self,
            "ApiLogGroup",
            log_group_name=f"/{project_tag}/api",
            retention=logs.RetentionDays.TWO_WEEKS,
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ApiLogs",
                actions=[
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "logs:DescribeLogStreams",
                ],
                resources=[
                    f"arn:aws:logs:{self.region}:{self.account}:log-group:/{project_tag}/api",
                    f"arn:aws:logs:{self.region}:{self.account}:log-group:/{project_tag}/api:*",
                ],
            )
        )

        bundle = _stage_api_bundle()
        code_asset = s3_assets.Asset(
            self,
            "ApiSourceAsset",
            path=str(bundle),
            ignore_mode=IgnoreMode.GLOB,
            exclude=["**/.env", "**/.env.*"],
        )
        code_asset.grant_read(role)

        user_data = ec2.UserData.for_linux()
        user_data.add_commands(
            "set -euxo pipefail",
            "dnf install -y unzip awscli",
            "mkdir -p /opt/trade-recon",
        )
        local_zip = user_data.add_s3_download_command(
            bucket=code_asset.bucket,
            bucket_key=code_asset.s3_object_key,
            local_file="/opt/trade-recon/src.zip",
        )
        user_data.add_commands(
            "rm -rf /opt/trade-recon/app",
            "mkdir -p /opt/trade-recon/app",
            f"unzip -o {local_zip} -d /opt/trade-recon/app",
            f"export TRADE_RECON_SSM_PARAM={ssm_parameter_name}",
            f"export CLOUDFRONT_ORIGIN={cloudfront_origin}",
            f"export S3_CACHE_BUCKET={existing_market_data_bucket}",
            f"export AWS_REGION={self.region}",
            Fn.sub("export COGNITO_USER_POOL_ID=${id}", {"id": cognito_user_pool_id or ""}),
            Fn.sub("export COGNITO_CLIENT_ID=${id}", {"id": cognito_client_id or ""}),
            f"export COGNITO_REGION={self.region}",
            Fn.sub("export SCHEDULER_SECRET_ARN=${arn}", {"arn": scheduler_secret.secret_arn}),
            "chmod +x /opt/trade-recon/app/infra/ec2_api/bootstrap.sh",
            "bash /opt/trade-recon/app/infra/ec2_api/bootstrap.sh",
        )

        instance = ec2.Instance(
            self,
            "ApiInstance",
            instance_type=ec2.InstanceType("t4g.micro"),
            machine_image=ec2.MachineImage.from_ssm_parameter(
                "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64",
                os=ec2.OperatingSystemType.LINUX,
            ),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PUBLIC,
                availability_zones=["us-east-1a"],
                subnet_filters=[ec2.SubnetFilter.by_ids([api_subnet_id])],
            ),
            security_group=api_sg,
            role=role,
            user_data=user_data,
            user_data_causes_replacement=True,
            associate_public_ip_address=True,
            require_imdsv2=True,
            detailed_monitoring=False,
        )
        Tags.of(instance).add("Name", f"{project_tag}-api")

        eip = ec2.CfnEIP(
            self,
            "ApiEip",
            domain="vpc",
            tags=[{"key": "Project", "value": project_tag}, {"key": "Name", "value": f"{project_tag}-api-eip"}],
        )
        ec2.CfnEIPAssociation(
            self,
            "ApiEipAssociation",
            allocation_id=eip.attr_allocation_id,
            instance_id=instance.instance_id,
        )

        # CloudFront custom origins cannot be raw IPs; us-east-1 public DNS is
        # ec2-A-B-C-D.compute-1.amazonaws.com for the Elastic IP.
        self.api_origin_domain = Fn.join(
            "",
            [
                "ec2-",
                Fn.join("-", Fn.split(".", eip.attr_public_ip)),
                ".compute-1.amazonaws.com",
            ],
        )
        self.api_security_group = api_sg
        self.instance = instance
        self.scheduler_secret = scheduler_secret

        CfnOutput(self, "ApiElasticIp", value=eip.attr_public_ip)
        CfnOutput(self, "ApiOriginDns", value=self.api_origin_domain)
        CfnOutput(self, "ApiPublicHttpUrl", value=f"http://{eip.attr_public_ip}")
        CfnOutput(self, "ApiHealthUrlDirect", value=f"http://{eip.attr_public_ip}/health")
        CfnOutput(
            self,
            "ApiHealthUrlCloudFront",
            value=f"{cloudfront_origin.rstrip('/')}/health",
        )
        CfnOutput(self, "DatabaseUrlSsmParameter", value=ssm_parameter_name)
        CfnOutput(
            self,
            "RdsSgNote",
            value=(
                "RDS SG ingress: 5432 from this API SG + existing laptop /32. "
                "Never 0.0.0.0/0 on 5432. API :80 from CloudFront prefix list only."
            ),
        )
        CfnOutput(
            self,
            "PutDatabaseUrlHint",
            value=(
                "Before first boot succeeds: AWS_PROFILE=trade-recon-8948 "
                "uv run python infra/ec2_api/put_database_url.py"
            ),
        )
