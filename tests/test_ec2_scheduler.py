from __future__ import annotations

from typing import Any

import boto3
import pytest
from moto import mock_aws

from conftest import REGION, load_handler

scheduler = load_handler("ec2_scheduler")

AMI_ID = "ami-12345678"  # moto accepts any AMI-shaped ID by default


def _launch(ec2: Any, tags: dict[str, str] | None = None) -> str:
    kwargs: dict[str, Any] = {"ImageId": AMI_ID, "MinCount": 1, "MaxCount": 1, "InstanceType": "t3.micro"}
    if tags:
        kwargs["TagSpecifications"] = [
            {"ResourceType": "instance", "Tags": [{"Key": k, "Value": v} for k, v in tags.items()]}
        ]
    return ec2.run_instances(**kwargs)["Instances"][0]["InstanceId"]


def _state(ec2: Any, instance_id: str) -> str:
    resp = ec2.describe_instances(InstanceIds=[instance_id])
    return resp["Reservations"][0]["Instances"][0]["State"]["Name"]


@pytest.fixture
def ec2():
    with mock_aws():
        yield boto3.client("ec2", region_name=REGION)


def test_stop_only_stops_tagged_running_instances(ec2: Any) -> None:
    tagged = _launch(ec2, {"Schedule": "office-hours"})
    other_value = _launch(ec2, {"Schedule": "always-on"})
    untagged = _launch(ec2)

    result = scheduler.lambda_handler({"action": "stop"})

    assert result["action"] == "stop"
    assert result["instance_ids"] == [tagged]
    assert _state(ec2, tagged) == "stopped"
    assert _state(ec2, other_value) == "running"
    assert _state(ec2, untagged) == "running"


def test_start_only_starts_tagged_stopped_instances(ec2: Any) -> None:
    tagged = _launch(ec2, {"Schedule": "office-hours"})
    untagged = _launch(ec2)
    ec2.stop_instances(InstanceIds=[tagged, untagged])

    result = scheduler.lambda_handler({"action": "START"})

    assert result["instance_ids"] == [tagged]
    assert _state(ec2, tagged) == "running"
    assert _state(ec2, untagged) == "stopped"


def test_stop_ignores_already_stopped_instances(ec2: Any) -> None:
    tagged = _launch(ec2, {"Schedule": "office-hours"})
    ec2.stop_instances(InstanceIds=[tagged])

    result = scheduler.lambda_handler({"action": "stop"})

    assert result["instance_count"] == 0
    assert result["instance_ids"] == []


def test_dry_run_does_not_change_state(ec2: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DRY_RUN", "true")
    tagged = _launch(ec2, {"Schedule": "office-hours"})

    result = scheduler.lambda_handler({"action": "stop"})

    assert result["dry_run"] is True
    assert result["instance_ids"] == [tagged]
    assert _state(ec2, tagged) == "running"


def test_custom_tag_from_environment(ec2: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCHEDULE_TAG_KEY", "AutoStop")
    monkeypatch.setenv("SCHEDULE_TAG_VALUE", "yes")
    custom = _launch(ec2, {"AutoStop": "yes"})
    default_tagged = _launch(ec2, {"Schedule": "office-hours"})

    result = scheduler.lambda_handler({"action": "stop"})

    assert result["instance_ids"] == [custom]
    assert _state(ec2, default_tagged) == "running"


@pytest.mark.parametrize("event", [{}, {"action": "reboot"}, {"action": ""}])
def test_invalid_action_raises(ec2: Any, event: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        scheduler.lambda_handler(event)
