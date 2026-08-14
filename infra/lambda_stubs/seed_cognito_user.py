"""CloudFormation on-event: seed one demo analyst into the Cognito user pool."""

from __future__ import annotations

import json
from typing import Any

import boto3
from botocore.exceptions import ClientError


def _password(secret_arn: str) -> str:
    sm = boto3.client("secretsmanager")
    raw = sm.get_secret_value(SecretId=secret_arn)["SecretString"]
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return raw
        if isinstance(parsed, dict) and "password" in parsed:
            return str(parsed["password"])
    return raw


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    request_type = event["RequestType"]
    props = event["ResourceProperties"]
    pool_id = props["UserPoolId"]
    username = props["Username"]
    email = props["Email"]
    secret_arn = props["PasswordSecretArn"]
    ssm_name = props.get("SsmParameterName") or "/trade-recon/demo-analyst-password"
    physical_id = f"{pool_id}/{username}"

    cognito = boto3.client("cognito-idp")
    ssm = boto3.client("ssm")

    if request_type == "Delete":
        try:
            cognito.admin_delete_user(UserPoolId=pool_id, Username=username)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "UserNotFoundException":
                raise
        return {"PhysicalResourceId": event.get("PhysicalResourceId") or physical_id}

    password = _password(secret_arn)
    try:
        cognito.admin_create_user(
            UserPoolId=pool_id,
            Username=username,
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
            ],
            MessageAction="SUPPRESS",
            TemporaryPassword=password,
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "UsernameExistsException":
            raise

    cognito.admin_set_user_password(
        UserPoolId=pool_id,
        Username=username,
        Password=password,
        Permanent=True,
    )
    ssm.put_parameter(
        Name=ssm_name,
        Value=password,
        Type="SecureString",
        Overwrite=True,
    )
    return {"PhysicalResourceId": physical_id, "Data": {"Username": username}}
