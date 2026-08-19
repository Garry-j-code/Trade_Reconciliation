"""Scheduled daily blotter + memory writer + 30-day sunset pause."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from aws_cdk import (
    CfnOutput,
    Duration,
    Stack,
    Tags,
)
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_ssm as ssm
from aws_cdk import aws_stepfunctions as sfn
from constructs import Construct

STUB_DIR = Path(__file__).resolve().parents[1] / "lambda_stubs"
ASL_PATH = Path(__file__).resolve().parents[1] / "step_functions" / "recon_pipeline.asl.json"


class PipelineStack(Stack):
    """EventBridge → Lambda → SSM daily blotter. Sunset watcher stops compute."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        project_tag: str = "trade-recon",
        api_base_url: str = "https://d1a8rtzx54qkw.cloudfront.net",
        instance_id: Optional[str] = None,
        scheduler_secret: Optional[secretsmanager.ISecret] = None,
        rds_identifier: str = "trade-recon-postgres",
        sunset_days: int = 30,
        sunset_date: Optional[str] = None,
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        Tags.of(self).add("Project", project_tag)

        # Address the API box by Name tag: a deploy of the API stack replaces
        # the instance, so a hardcoded id goes stale until this stack is
        # redeployed. instance_id stays an explicit override.
        instance_name_tag = f"{project_tag}-api"
        environment = {
            "PROJECT": project_tag,
            "API_BASE_URL": api_base_url.rstrip("/"),
            "INSTANCE_NAME_TAG": instance_name_tag,
        }
        if instance_id:
            environment["INSTANCE_ID"] = instance_id
        if scheduler_secret is not None:
            environment["SCHEDULER_SECRET_ARN"] = scheduler_secret.secret_arn

        trigger_fn = lambda_.Function(
            self,
            "ReconTriggerFunction",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="recon_trigger.handler",
            code=lambda_.Code.from_asset(str(STUB_DIR)),
            timeout=Duration.seconds(180),
            memory_size=128,
            description="SSM daily-blotter + HITL memory backfill",
            environment=environment,
        )
        if scheduler_secret is not None:
            scheduler_secret.grant_read(trigger_fn)
        trigger_fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="SsmSendCommandDocument",
                actions=["ssm:SendCommand"],
                resources=[f"arn:aws:ssm:{self.region}::document/AWS-RunShellScript"],
            )
        )
        # Scoped by tag, not instance id, so it survives instance replacement.
        trigger_fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="SsmSendCommandTaggedInstance",
                actions=["ssm:SendCommand"],
                resources=[f"arn:aws:ec2:{self.region}:{self.account}:instance/*"],
                conditions={"StringEquals": {"ssm:resourceTag/Name": instance_name_tag}},
            )
        )

        asl = json.loads(ASL_PATH.read_text(encoding="utf-8"))
        state_machine = sfn.StateMachine(
            self,
            "ReconPipeline",
            state_machine_name=f"{project_tag}-recon-pipeline",
            definition_body=sfn.DefinitionBody.from_string(json.dumps(asl)),
            definition_substitutions={
                "TriggerFunctionArn": trigger_fn.function_arn,
            },
            timeout=Duration.minutes(5),
            comment="Manual start: Lambda SSM daily-blotter",
        )
        trigger_fn.grant_invoke(state_machine)

        events.Rule(
            self,
            "WeekdayReconRule",
            rule_name=f"{project_tag}-daily-blotter",
            description="Weekdays 21:30 UTC — daily blotter via SSM (after US equity close)",
            schedule=events.Schedule.cron(minute="30", hour="21", week_day="MON-FRI"),
            targets=[
                targets.LambdaFunction(
                    trigger_fn,
                    event=events.RuleTargetInput.from_object({"action": "blotter"}),
                )
            ],
        )

        if instance_id:
            events.Rule(
                self,
                "DailyMemoryRule",
                rule_name=f"{project_tag}-daily-memory",
                description="Daily 07:00 UTC — agent memory backfill (Titan embed if needed; skip if caught up)",
                schedule=events.Schedule.cron(minute="0", hour="7"),
                targets=[
                    targets.LambdaFunction(
                        trigger_fn,
                        event=events.RuleTargetInput.from_object({"action": "memory"}),
                    )
                ],
            )

        pinned = (sunset_date or "").strip()[:10]
        sunset = pinned or (
            datetime.now(timezone.utc) + timedelta(days=max(1, int(sunset_days)))
        ).date().isoformat()
        sunset_param = ssm.StringParameter(
            self,
            "ProductSunsetDate",
            parameter_name="/trade-recon/product-sunset-date",
            string_value=sunset,
            description="ISO date when the sunset watcher stops EC2+RDS and disables rules",
        )
        rule_names = [
            f"{project_tag}-daily-blotter",
            f"{project_tag}-daily-memory",
            f"{project_tag}-sunset-watch",
            f"{project_tag}-weekday-recon",
        ]
        stop_fn = lambda_.Function(
            self,
            "SunsetStopFunction",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="stop_billing.handler",
            code=lambda_.Code.from_asset(str(STUB_DIR)),
            timeout=Duration.seconds(60),
            memory_size=128,
            description="Stop EC2 + RDS and disable EventBridge rules (no destroy)",
            environment={
                "SUNSET_PARAM": sunset_param.parameter_name,
                "RDS_ID": rds_identifier,
                "INSTANCE_ID": instance_id or "",
                "INSTANCE_NAME_TAG": instance_name_tag,
                "RULE_NAMES": ",".join(rule_names),
            },
        )
        sunset_param.grant_read(stop_fn)
        stop_fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="StopCompute",
                actions=["ec2:StopInstances", "ec2:DescribeInstances"],
                resources=["*"],
            )
        )
        stop_fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="StopRds",
                actions=["rds:StopDBInstance", "rds:DescribeDBInstances"],
                resources=[
                    f"arn:aws:rds:{self.region}:{self.account}:db:{rds_identifier}"
                ],
            )
        )
        stop_fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="DisableRules",
                actions=["events:DisableRule", "events:DescribeRule"],
                resources=[
                    f"arn:aws:events:{self.region}:{self.account}:rule/{name}"
                    for name in rule_names
                ],
            )
        )
        events.Rule(
            self,
            "SunsetWatchRule",
            rule_name=f"{project_tag}-sunset-watch",
            description="Daily check of /trade-recon/product-sunset-date then pause compute",
            schedule=events.Schedule.cron(minute="0", hour="12"),
            targets=[targets.LambdaFunction(stop_fn)],
        )

        self.trigger_function = trigger_fn
        self.state_machine = state_machine

        CfnOutput(self, "StateMachineArn", value=state_machine.state_machine_arn)
        CfnOutput(self, "ReconTriggerFunctionArn", value=trigger_fn.function_arn)
        CfnOutput(
            self,
            "BlotterSchedule",
            value="cron 21:30 UTC Mon-Fri (EventBridge → SSM daily-blotter on EC2)",
        )
        CfnOutput(
            self,
            "MemorySchedule",
            value="cron 07:00 UTC daily (EventBridge → SSM Run Command, --provider stub)",
        )
        CfnOutput(self, "SunsetDateValue", value=sunset)
        CfnOutput(self, "ProductSunsetParam", value="/trade-recon/product-sunset-date")
        CfnOutput(
            self,
            "ExampleStartExecutionInput",
            value=json.dumps({"action": "blotter"}),
        )
