"""Report-only cost hygiene scan for the current account and region.

Finds resources that commonly cause "silent" spend and publishes a summary to
an SNS topic. This function never modifies or deletes anything - a human
reviews the report and decides what to clean up.

Checks:
    * EBS volumes in the ``available`` state (not attached to any instance)
    * Elastic IPs that are not associated with an instance or ENI
    * EBS snapshots owned by this account older than ``SNAPSHOT_MAX_AGE_DAYS``

Environment variables:
    SNS_TOPIC_ARN          Topic to publish the report to (required)
    SNAPSHOT_MAX_AGE_DAYS  Age threshold for snapshots (default: ``90``)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Rough us-east-1 list prices used only to give the reader a sense of scale.
GP_VOLUME_USD_PER_GB_MONTH = 0.08
IDLE_EIP_USD_PER_MONTH = 3.60
SNAPSHOT_USD_PER_GB_MONTH = 0.05

SNS_SUBJECT_MAX_LEN = 100


@dataclass
class HygieneReport:
    region: str
    snapshot_max_age_days: int
    unattached_volumes: list[dict[str, Any]] = field(default_factory=list)
    unassociated_eips: list[dict[str, Any]] = field(default_factory=list)
    old_snapshots: list[dict[str, Any]] = field(default_factory=list)

    @property
    def finding_count(self) -> int:
        return len(self.unattached_volumes) + len(self.unassociated_eips) + len(self.old_snapshots)

    @property
    def estimated_monthly_usd(self) -> float:
        volumes = sum(v["SizeGiB"] for v in self.unattached_volumes) * GP_VOLUME_USD_PER_GB_MONTH
        eips = len(self.unassociated_eips) * IDLE_EIP_USD_PER_MONTH
        # Snapshot storage is incremental, so VolumeSize is an upper bound.
        snaps = sum(s["VolumeSizeGiB"] for s in self.old_snapshots) * SNAPSHOT_USD_PER_GB_MONTH
        return round(volumes + eips + snaps, 2)


def _name_tag(tags: list[dict[str, str]] | None) -> str:
    for tag in tags or []:
        if tag.get("Key") == "Name":
            return tag.get("Value", "")
    return ""


def find_unattached_volumes(ec2_client: Any) -> list[dict[str, Any]]:
    paginator = ec2_client.get_paginator("describe_volumes")
    results: list[dict[str, Any]] = []
    for page in paginator.paginate(Filters=[{"Name": "status", "Values": ["available"]}]):
        for vol in page.get("Volumes", []):
            results.append(
                {
                    "VolumeId": vol["VolumeId"],
                    "SizeGiB": vol["Size"],
                    "VolumeType": vol.get("VolumeType", ""),
                    "CreateTime": vol["CreateTime"].isoformat(),
                    "Name": _name_tag(vol.get("Tags")),
                }
            )
    return results


def find_unassociated_eips(ec2_client: Any) -> list[dict[str, Any]]:
    response = ec2_client.describe_addresses()
    results: list[dict[str, Any]] = []
    for addr in response.get("Addresses", []):
        if addr.get("AssociationId") or addr.get("InstanceId") or addr.get("NetworkInterfaceId"):
            continue
        results.append(
            {
                "AllocationId": addr.get("AllocationId", ""),
                "PublicIp": addr.get("PublicIp", ""),
                "Name": _name_tag(addr.get("Tags")),
            }
        )
    return results


def find_old_snapshots(ec2_client: Any, max_age_days: int, now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max_age_days)
    paginator = ec2_client.get_paginator("describe_snapshots")
    results: list[dict[str, Any]] = []
    for page in paginator.paginate(OwnerIds=["self"]):
        for snap in page.get("Snapshots", []):
            start_time: datetime = snap["StartTime"]
            if start_time.tzinfo is None:
                start_time = start_time.replace(tzinfo=timezone.utc)
            if start_time < cutoff:
                results.append(
                    {
                        "SnapshotId": snap["SnapshotId"],
                        "VolumeSizeGiB": snap.get("VolumeSize", 0),
                        "StartTime": start_time.isoformat(),
                        "AgeDays": (now - start_time).days,
                        "Description": snap.get("Description", ""),
                    }
                )
    return results


def build_report(ec2_client: Any, region: str, max_age_days: int, now: datetime | None = None) -> HygieneReport:
    report = HygieneReport(region=region, snapshot_max_age_days=max_age_days)
    report.unattached_volumes = find_unattached_volumes(ec2_client)
    report.unassociated_eips = find_unassociated_eips(ec2_client)
    report.old_snapshots = find_old_snapshots(ec2_client, max_age_days, now=now)
    return report


def format_report(report: HygieneReport) -> str:
    lines = [
        f"Cost hygiene report for region {report.region}",
        "Report only - no resources were modified.",
        "",
        f"Total findings: {report.finding_count}",
        f"Rough monthly cost of findings: ~${report.estimated_monthly_usd:.2f} USD",
        "",
        f"Unattached EBS volumes ({len(report.unattached_volumes)}):",
    ]
    lines += [
        f"  - {v['VolumeId']} {v['SizeGiB']} GiB {v['VolumeType']} created {v['CreateTime']} {v['Name']}".rstrip()
        for v in report.unattached_volumes
    ] or ["  none"]

    lines += ["", f"Unassociated Elastic IPs ({len(report.unassociated_eips)}):"]
    lines += [
        f"  - {e['PublicIp']} ({e['AllocationId']}) {e['Name']}".rstrip() for e in report.unassociated_eips
    ] or ["  none"]

    lines += ["", f"Snapshots older than {report.snapshot_max_age_days} days ({len(report.old_snapshots)}):"]
    lines += [
        f"  - {s['SnapshotId']} {s['VolumeSizeGiB']} GiB, {s['AgeDays']} days old {s['Description']}".rstrip()
        for s in report.old_snapshots
    ] or ["  none"]

    return "\n".join(lines)


def lambda_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    topic_arn = os.environ.get("SNS_TOPIC_ARN")
    if not topic_arn:
        raise RuntimeError("SNS_TOPIC_ARN environment variable is required")
    max_age_days = int(os.environ.get("SNAPSHOT_MAX_AGE_DAYS", "90"))

    session = boto3.session.Session()
    region = session.region_name or "unknown"
    ec2_client = session.client("ec2")
    sns_client = session.client("sns")

    report = build_report(ec2_client, region, max_age_days)
    body = format_report(report)
    logger.info(
        "Findings: volumes=%d eips=%d snapshots=%d est_monthly_usd=%.2f",
        len(report.unattached_volumes),
        len(report.unassociated_eips),
        len(report.old_snapshots),
        report.estimated_monthly_usd,
    )

    subject = f"[cost-hygiene] {region}: {report.finding_count} finding(s)"[:SNS_SUBJECT_MAX_LEN]
    sns_client.publish(TopicArn=topic_arn, Subject=subject, Message=body)
    logger.info("Published report to %s", topic_arn)

    return {
        "region": region,
        "finding_count": report.finding_count,
        "unattached_volumes": len(report.unattached_volumes),
        "unassociated_eips": len(report.unassociated_eips),
        "old_snapshots": len(report.old_snapshots),
        "estimated_monthly_usd": report.estimated_monthly_usd,
    }
