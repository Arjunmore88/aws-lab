# Runbook: ASG instances failing health checks

| | |
|---|---|
| **Alarm** | `aws-lab-<env>-unhealthy-hosts` |
| **Severity** | Medium (High if healthy count reaches 0) |
| **Symptom** | Instances cycle in and out of service; ASG keeps replacing them |

The ASG uses **ELB health checks** (`HealthCheckType: ELB`), so an instance
that fails the target group check (`GET /health`, expect `200`) for 3
consecutive 15-second intervals is marked unhealthy and replaced after the
180-second grace period.

## 1. Which targets are unhealthy and why

```bash
ENV=dev REGION=ap-south-1
TG_ARN=$(aws elbv2 describe-target-groups --region "$REGION" --names "aws-lab-$ENV-tg" \
  --query 'TargetGroups[0].TargetGroupArn' --output text)
aws elbv2 describe-target-health --region "$REGION" --target-group-arn "$TG_ARN" --output table
```

| `TargetHealth.Reason` | Likely cause |
|---|---|
| `Target.Timeout` | Security group or NACL blocking ALB -> instance on port 80, or nginx not listening |
| `Target.ResponseCodeMismatch` | `/health` returns 404/500 - user data did not finish or the health file is missing |
| `Target.FailedHealthChecks` | nginx stopped, crashed, or the instance is overloaded |
| `Elb.InitialHealthChecking` | Normal for a new instance - wait |

## 2. Check recent scaling activity

```bash
aws autoscaling describe-scaling-activities --region "$REGION" \
  --auto-scaling-group-name "aws-lab-$ENV-web-asg" --max-items 10 \
  --query 'Activities[].[StartTime,StatusCode,Description,Cause]' --output table
```

Repeated "terminating ... failed ELB health check" entries confirm a boot or
app problem rather than a one-off failure.

## 3. Inspect an instance

```bash
aws ssm start-session --region "$REGION" --target <instance-id>
sudo cat /var/log/cloud-init-output.log | tail -n 50   # did user data succeed?
sudo systemctl status nginx
curl -s -o /dev/null -w '%{http_code}\n' http://localhost/health
```

If Session Manager cannot connect, the instance probably cannot reach the SSM
endpoints - check that the NAT Gateway exists (`EnableNatGateway=true`) and the
private route table has a `0.0.0.0/0` route to it.

If user data failed on `dnf install`, the instance had no outbound path to the
package repositories. AL2023 repos are served from S3, so the S3 gateway
endpoint should cover this even without NAT - confirm the endpoint is attached
to the private route table.

## 4. Stop the replacement loop while debugging

Suspend health-check-driven replacement so a broken instance stays up long
enough to investigate:

```bash
aws autoscaling suspend-processes --region "$REGION" \
  --auto-scaling-group-name "aws-lab-$ENV-web-asg" --scaling-processes ReplaceUnhealthy
```

**Resume it when done** - leaving it suspended removes self-healing:

```bash
aws autoscaling resume-processes --region "$REGION" \
  --auto-scaling-group-name "aws-lab-$ENV-web-asg" --scaling-processes ReplaceUnhealthy
```

## 5. Fix and verify

- Fix the launch template / user data in `cloudformation/app.yaml` and redeploy;
  the rolling update policy replaces instances one at a time.
- Verify all targets are `healthy` and the alarm returns to `OK`.
