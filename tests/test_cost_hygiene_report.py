from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import boto3
import pytest
from moto import mock_aws

from conftest import REGION, load_handler

report_mod = load_handler("cost_hygiene_report")


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch):
    with mock_aws():
        ec2 = boto3.client("ec2", region_name=REGION)
        sns = boto3.client("sns", region_name=REGION)
        sqs = boto3.client("sqs", region_name=REGION)

        topic_arn = sns.create_topic(Name="aws-lab-dev-alerts")["TopicArn"]
        # Subscribe a queue so the published message can be inspected.
        queue_url = sqs.create_queue(QueueName="report-capture")["QueueUrl"]
        queue_arn = sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["QueueArn"])["Attributes"][
            "QueueArn"
        ]
        sns.subscribe(TopicArn=topic_arn, Protocol="sqs", Endpoint=queue_arn)

        monkeypatch.setenv("SNS_TOPIC_ARN", topic_arn)
        yield {"ec2": ec2, "sqs": sqs, "queue_url": queue_url}


def _received_messages(aws: dict[str, Any]) -> list[str]:
    msgs = aws["sqs"].receive_message(QueueUrl=aws["queue_url"], MaxNumberOfMessages=10).get("Messages", [])
    return [m["Body"] for m in msgs]


def test_empty_account_reports_zero_findings(aws: dict[str, Any]) -> None:
    result = report_mod.lambda_handler({})

    assert result["finding_count"] == 0
    assert result["estimated_monthly_usd"] == 0
    bodies = _received_messages(aws)
    assert len(bodies) == 1
    assert "Total findings: 0" in bodies[0]


def test_detects_unattached_volume_but_not_attached(aws: dict[str, Any]) -> None:
    ec2 = aws["ec2"]
    loose = ec2.create_volume(AvailabilityZone=f"{REGION}a", Size=20, VolumeType="gp3")["VolumeId"]
    instance_id = ec2.run_instances(ImageId="ami-12345678", MinCount=1, MaxCount=1)["Instances"][0]["InstanceId"]
    attached = ec2.create_volume(AvailabilityZone=f"{REGION}a", Size=5)["VolumeId"]
    ec2.attach_volume(VolumeId=attached, InstanceId=instance_id, Device="/dev/sdf")

    volumes = report_mod.find_unattached_volumes(ec2)

    ids = {v["VolumeId"] for v in volumes}
    assert loose in ids
    assert attached not in ids


def test_detects_only_unassociated_eips(aws: dict[str, Any]) -> None:
    ec2 = aws["ec2"]
    idle = ec2.allocate_address(Domain="vpc")
    in_use = ec2.allocate_address(Domain="vpc")
    instance_id = ec2.run_instances(ImageId="ami-12345678", MinCount=1, MaxCount=1)["Instances"][0]["InstanceId"]
    ec2.associate_address(AllocationId=in_use["AllocationId"], InstanceId=instance_id)

    eips = report_mod.find_unassociated_eips(ec2)

    assert [e["AllocationId"] for e in eips] == [idle["AllocationId"]]


def test_old_snapshot_threshold(aws: dict[str, Any]) -> None:
    ec2 = aws["ec2"]
    vol = ec2.create_volume(AvailabilityZone=f"{REGION}a", Size=8)["VolumeId"]
    snap_id = ec2.create_snapshot(VolumeId=vol, Description="nightly")["SnapshotId"]

    # moto stamps snapshots with the current time, so evaluate "now" in the future.
    # moto also pre-populates AMI snapshots, so assert on membership only.
    future = datetime.now(timezone.utc) + timedelta(days=120)
    old = report_mod.find_old_snapshots(ec2, 90, now=future)
    assert snap_id in {s["SnapshotId"] for s in old}
    assert all(s["AgeDays"] >= 90 for s in old)

    current = report_mod.find_old_snapshots(ec2, 90)
    assert snap_id not in {s["SnapshotId"] for s in current}


def test_full_report_publishes_summary_and_never_deletes(aws: dict[str, Any]) -> None:
    ec2 = aws["ec2"]
    vol = ec2.create_volume(AvailabilityZone=f"{REGION}a", Size=100, VolumeType="gp3")["VolumeId"]
    eip = ec2.allocate_address(Domain="vpc")

    result = report_mod.lambda_handler({})

    assert result["unattached_volumes"] == 1
    assert result["unassociated_eips"] == 1
    assert result["finding_count"] == 2
    assert result["estimated_monthly_usd"] == pytest.approx(100 * 0.08 + 3.60)

    body = _received_messages(aws)[0]
    assert vol in body
    assert eip["PublicIp"] in body
    assert "Report only" in body

    # Report-only: resources must still exist afterwards.
    assert ec2.describe_volumes(VolumeIds=[vol])["Volumes"][0]["State"] == "available"
    assert len(ec2.describe_addresses(AllocationIds=[eip["AllocationId"]])["Addresses"]) == 1


def test_format_report_lists_each_section(aws: dict[str, Any]) -> None:
    report = report_mod.build_report(aws["ec2"], REGION, 30)
    text = report_mod.format_report(report)
    assert "Unattached EBS volumes (0):" in text
    assert "Unassociated Elastic IPs (0):" in text
    assert "Snapshots older than 30 days" in text


def test_missing_topic_env_var_raises(aws: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SNS_TOPIC_ARN")
    with pytest.raises(RuntimeError):
        report_mod.lambda_handler({})
