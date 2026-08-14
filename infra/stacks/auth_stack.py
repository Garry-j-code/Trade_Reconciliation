"""Amazon Cognito user pool + one seeded demo analyst (password in Secrets Manager / SSM)."""

from __future__ import annotations

from pathlib import Path

from aws_cdk import CfnOutput, CustomResource, Duration, RemovalPolicy, Stack, Tags
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import custom_resources as cr
from constructs import Construct

STUB_DIR = Path(__file__).resolve().parents[1] / "lambda_stubs"
DEMO_USERNAME = "analyst@traderecon.demo"
DEMO_PASSWORD_SSM = "/trade-recon/demo-analyst-password"


class AuthStack(Stack):
    """Email/password user pool. No hosted UI — the React app owns login."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        project_tag: str = "trade-recon",
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        Tags.of(self).add("Project", project_tag)

        user_pool = cognito.UserPool(
            self,
            "UserPool",
            user_pool_name=f"{project_tag}-users",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            password_policy=cognito.PasswordPolicy(
                min_length=12,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=True,
            ),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            mfa=cognito.Mfa.OFF,
            deletion_protection=False,
            removal_policy=RemovalPolicy.DESTROY,
        )

        client = user_pool.add_client(
            "DashboardClient",
            user_pool_client_name=f"{project_tag}-dashboard",
            generate_secret=False,
            auth_flows=cognito.AuthFlow(
                user_password=True,
                user_srp=True,
            ),
            access_token_validity=Duration.hours(1),
            id_token_validity=Duration.hours(1),
            refresh_token_validity=Duration.days(7),
            prevent_user_existence_errors=True,
            enable_token_revocation=True,
        )

        demo_password = secretsmanager.Secret(
            self,
            "DemoAnalystPassword",
            secret_name=f"{project_tag}/demo-analyst-password",
            description="Initial demo analyst password — change after handover",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                password_length=20,
                exclude_characters=" \"'\\@/<>",
                require_each_included_type=True,
                include_space=False,
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )

        seed_fn = lambda_.Function(
            self,
            "SeedDemoUserFn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="seed_cognito_user.handler",
            code=lambda_.Code.from_asset(str(STUB_DIR)),
            timeout=Duration.seconds(60),
            memory_size=128,
            description="Create/reset the single demo Cognito analyst",
        )
        demo_password.grant_read(seed_fn)
        seed_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "cognito-idp:AdminCreateUser",
                    "cognito-idp:AdminSetUserPassword",
                    "cognito-idp:AdminDeleteUser",
                ],
                resources=[user_pool.user_pool_arn],
            )
        )
        seed_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ssm:PutParameter"],
                resources=[
                    f"arn:aws:ssm:{self.region}:{self.account}:parameter{DEMO_PASSWORD_SSM}"
                ],
            )
        )

        provider = cr.Provider(
            self,
            "SeedDemoUserProvider",
            on_event_handler=seed_fn,
        )
        CustomResource(
            self,
            "DemoAnalystUser",
            service_token=provider.service_token,
            properties={
                "UserPoolId": user_pool.user_pool_id,
                "Username": DEMO_USERNAME,
                "Email": DEMO_USERNAME,
                "PasswordSecretArn": demo_password.secret_arn,
                "SsmParameterName": DEMO_PASSWORD_SSM,
            },
        )

        self.user_pool = user_pool
        self.user_pool_id = user_pool.user_pool_id
        self.client_id = client.user_pool_client_id
        self.demo_username = DEMO_USERNAME
        self.demo_password_ssm = DEMO_PASSWORD_SSM
        self.demo_password_secret_arn = demo_password.secret_arn

        CfnOutput(self, "UserPoolId", value=user_pool.user_pool_id)
        CfnOutput(self, "UserPoolClientId", value=client.user_pool_client_id)
        CfnOutput(self, "DemoAnalystUsername", value=DEMO_USERNAME)
        CfnOutput(
            self,
            "DemoAnalystPasswordSsm",
            value=DEMO_PASSWORD_SSM,
            description="SecureString — aws ssm get-parameter --with-decryption (not in git)",
        )
        CfnOutput(self, "DemoAnalystPasswordSecretArn", value=demo_password.secret_arn)
