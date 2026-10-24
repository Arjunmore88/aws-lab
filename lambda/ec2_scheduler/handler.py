"""Stop or start EC2 instances that opt in to an office-hours schedule.

Instances opt in by carrying the tag ``Schedule=office-hours`` (tag key and
value are configurable). EventBridge Scheduler invokes this function with a
payload such as ``{"action": "stop"}`` in the evening and
``{"action": "start"}`` in the morning.

Environment variables:
    SCHEDULE_TAG_KEY    Tag key to match (default: ``Schedule``)
    SCHEDULE_TAG_VALUE  Tag value to match (default: ``office-hours``)
    DRY_RUN             ``true`` to log intended actions without calling
                        StopInstances/StartInstances (default: ``false``)
"""

from __future__ import annotations

import logging
import os
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

VALID_ACTIONS = {"start", "stop"}

# The instance states each action should act on. Instances in any other state
# (pending, stopping, shutting-down, terminated) are left alone.
SOURCE_STATE = {"stop": "running", "start": "stopped"}


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def find_scheduled_instances(ec2_client: Any, tag_key: str, tag_value: str, state: str) -> list[str]:
    """Return IDs of instances with the schedule tag in the given state."""
    paginator = ec2_client.get_paginator("describe_instances")
    pages = paginator.paginate(
        Filters=[
            {"Name": f"tag:{tag_key}", "Values": [tag_value]},
            {"Name": "instance-state-name", "Values": [state]},
        ]
    )
    instance_ids: list[str] = []
    for page in pages:
        for reservation in page.get("Reservations", []):
            for instance in reservation.get("Instances", []):
                instance_ids.append(instance["InstanceId"])
    return instance_ids


def apply_action(ec2_client: Any, action: str, instance_ids: list[str], dry_run: bool) -> list[str]:
    """Stop or start the given instances. Returns the IDs acted on."""
    if not instance_ids:
        return []
    if dry_run:
        logger.info("DRY_RUN enabled - would %s instances: %s", action, instance_ids)
        return instance_ids

    if action == "stop":
        ec2_client.stop_instances(InstanceIds=instance_ids)
    else:
        ec2_client.start_instances(InstanceIds=instance_ids)
    logger.info("Requested %s for %d instance(s): %s", action, len(instance_ids), instance_ids)
    return instance_ids


def lambda_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    action = str(event.get("action", "")).strip().lower()
    if action not in VALID_ACTIONS:
        logger.error("Invalid or missing action in event: %s", event)
        raise ValueError(f"event.action must be one of {sorted(VALID_ACTIONS)}, got {action!r}")

    tag_key = os.environ.get("SCHEDULE_TAG_KEY", "Schedule")
    tag_value = os.environ.get("SCHEDULE_TAG_VALUE", "office-hours")
    dry_run = _env_flag("DRY_RUN")

    ec2_client = boto3.client("ec2")
    logger.info("Action=%s tag=%s=%s dry_run=%s", action, tag_key, tag_value, dry_run)

    try:
        targets = find_scheduled_instances(ec2_client, tag_key, tag_value, SOURCE_STATE[action])
        acted_on = apply_action(ec2_client, action, targets, dry_run)
    except ClientError:
        logger.exception("EC2 API call failed while trying to %s instances", action)
        raise

    if not acted_on:
        logger.info("No instances in state '%s' matched the schedule tag", SOURCE_STATE[action])

    return {
        "action": action,
        "dry_run": dry_run,
        "instance_count": len(acted_on),
        "instance_ids": acted_on,
    }
