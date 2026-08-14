"""Scheduled recon: EventBridge → Step Functions (one Task) → Lambda → CloudFront API."""

from __future__ import annotations

import json
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
from aws_cdk import aws_stepfunctions as sfn
from constructs import Construct

STUB_DIR = Path(__file__).resolve().parents[1] / "lambda_stubs"
ASL_PATH = Path(__file__).resolve().parents[1] / "step_functions" / "recon_pipeline.asl.json"


class PipelineStack(Stack):
    """Standard Step Functions with a single Lambda task. Near-zero $ at this volume."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        project_tag: str = "trade-recon",
        api_base_url: str = "https://d1a8rtzx54qkw.cloudfront.net",
        instance_id: Optional[str] = None,
        scheduler_secret: Optional[secretsmanager.ISecret] = None,
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        Tags.of(self).add("Project", project_tag)

        environment = {
            "PROJECT": project_tag,
            "API_BASE_URL": api_base_url.rstrip("/"),
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
            description="POST /api/recon/run via CloudFront; SSM memory writer (stub)",
            environment=environment,
        )
        if scheduler_secret is not None:
            scheduler_secret.grant_read(trigger_fn)
        if instance_id:
            trigger_fn.add_to_role_policy(
                iam.PolicyStatement(
                    sid="SsmSendCommand",
                    actions=["ssm:SendCommand"],
                    resources=[
                        f"arn:aws:ssm:{self.region}::document/AWS-RunShellScript",
                        f"arn:aws:ec2:{self.region}:{self.account}:instance/{instance_id}",
                    ],
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
            comment="Weekday recon: one Lambda POST to /api/recon/run",
        )
        trigger_fn.grant_invoke(state_machine)

        events.Rule(
            self,
            "WeekdayReconRule",
            rule_name=f"{project_tag}-weekday-recon",
            description="Weekdays 13:00 UTC — run reconciliation via Step Functions",
            schedule=events.Schedule.cron(minute="0", hour="13", week_day="MON-FRI"),
            targets=[
                targets.SfnStateMachine(
                    state_machine,
                    input=events.RuleTargetInput.from_object({"action": "recon"}),
                )
            ],
        )

        if instance_id:
            events.Rule(
                self,
                "DailyMemoryRule",
                rule_name=f"{project_tag}-daily-memory",
                description="Daily 07:00 UTC — agent memory writer (stub, no Bedrock)",
                schedule=events.Schedule.cron(minute="0", hour="7"),
                targets=[
                    targets.LambdaFunction(
                        trigger_fn,
                        event=events.RuleTargetInput.from_object({"action": "memory"}),
                    )
                ],
            )

        self.trigger_function = trigger_fn
        self.state_machine = state_machine

        CfnOutput(self, "StateMachineArn", value=state_machine.state_machine_arn)
        CfnOutput(self, "ReconTriggerFunctionArn", value=trigger_fn.function_arn)
        CfnOutput(
            self,
            "ReconSchedule",
            value="cron 13:00 UTC Mon-Fri (EventBridge → Step Functions → POST /api/recon/run)",
        )
        CfnOutput(
            self,
            "MemorySchedule",
            value="cron 07:00 UTC daily (EventBridge → SSM Run Command, --provider stub)",
        )
        CfnOutput(
            self,
            "ExampleStartExecutionInput",
            value=json.dumps({"action": "recon"}),
        )
