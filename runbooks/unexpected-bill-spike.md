# Runbook: Unexpected AWS bill spike

| | |
|---|---|
| **Trigger** | AWS Budgets alert, Cost Anomaly Detection email, or the weekly cost hygiene report |
| **Severity** | Medium - money is being spent, but service is not down |

## 1. Find what changed

```bash
ENV=dev REGION=ap-south-1
```

In **Billing > Cost Explorer**, group by *Service*, then by *Usage type*, with
daily granularity for the last 14 days. Or from the CLI:

```bash
aws ce get-cost-and-usage \
  --time-period Start=$(date -d '14 days ago' +%F),End=$(date +%F) \
  --granularity DAILY --metrics UnblendedCost \
  --group-by Type=DIMENSION,Key=SERVICE \
  --query 'ResultsByTime[].{day:TimePeriod.Start,groups:Groups[?Metrics.UnblendedCost.Amount>`1`]}'
```

Then filter by the `Project` and `Environment` cost allocation tags (activate
them once under Billing > Cost allocation tags) to see whether the spend is
from this workload.

## 2. Usual suspects for this architecture

| Usage type contains | Likely cause | Check |
|---|---|---|
| `NatGateway-Hours` / `NatGateway-Bytes` | NAT left running, or heavy traffic through NAT | Is a lab environment still deployed? Large downloads from instances? |
| `LoadBalancerUsage` / `LCUUsage` | ALB left running or traffic spike | `aws elbv2 describe-load-balancers` across regions |
| `BoxUsage` | More/larger EC2 instances than expected | ASG desired capacity, instances outside the ASG, scheduler not stopping dev instances |
| `EBS:VolumeUsage` | Unattached volumes | Cost hygiene report |
| `EBS:SnapshotUsage` | Old snapshots piling up | Cost hygiene report |
| `ElasticIP:IdleAddress` / `PublicIPv4` | Unassociated EIPs, public IPv4 charges | Cost hygiene report |
| `DataTransfer-Out` | Traffic spike, or data leaving the region | ALB `ProcessedBytes`, VPC Flow Logs |

Run the cost hygiene report on demand instead of waiting for the schedule:

```bash
aws lambda invoke --region "$REGION" \
  --function-name "aws-lab-$ENV-cost-hygiene-report" /dev/stdout
```

Check for resources in regions you do not normally use (a common sign of
leaked credentials):

```bash
for r in $(aws ec2 describe-regions --query 'Regions[].RegionName' --output text); do
  n=$(aws ec2 describe-instances --region "$r" \
      --filters Name=instance-state-name,Values=running \
      --query 'length(Reservations[].Instances[])' --output text)
  [ "$n" != "0" ] && echo "$r: $n running instance(s)"
done
```

## 3. Contain

- **Forgotten lab environment:** `scripts/destroy.sh -e <env> -r <region>`.
- **NAT not needed right now:** redeploy the network stack with `--no-nat`.
- **Dev instances running 24/7:** tag them `Schedule=office-hours` so the
  scheduler Lambda stops them each evening.
- **Unknown instances in odd regions:** treat as a security incident. Rotate
  access keys, review CloudTrail for `RunInstances`, and open an AWS Support case.
- **Idle volumes/EIPs/snapshots:** verify ownership with the tagged `Owner`,
  snapshot if needed, then delete manually. (The report Lambda never deletes.)

## 4. Prevent

- AWS Budgets with alerts at 50/80/100% of the monthly budget.
- Cost Anomaly Detection monitor on the linked account.
- Enforce the `Project`, `Environment`, `Owner`, `CostCenter` tags (Tag Policies
  or an SCP) so every resource has an accountable owner.
- Region guardrail policy: [`iam/deny-outside-approved-regions.json`](../iam/deny-outside-approved-regions.json).
