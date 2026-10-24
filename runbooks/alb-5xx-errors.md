# Runbook: ALB returning 5xx errors

| | |
|---|---|
| **Alarm** | `aws-lab-<env>-alb-5xx` |
| **Severity** | High - users are seeing errors |
| **Dashboard** | CloudWatch > Dashboards > `aws-lab-<env>-operations` |

## 1. Confirm and scope the impact

```bash
ENV=dev REGION=ap-south-1
ALB_URL=$(aws cloudformation describe-stacks --region "$REGION" --stack-name "aws-lab-$ENV-app" \
  --query "Stacks[0].Outputs[?OutputKey=='LoadBalancerUrl'].OutputValue" --output text)
for i in $(seq 1 10); do curl -s -o /dev/null -w '%{http_code}\n' "$ALB_URL"; done
```

Check the dashboard "ALB 4xx / 5xx" widget to see **which** metric is rising:

| Metric rising | Meaning | Go to |
|---|---|---|
| `HTTPCode_ELB_5XX_Count` (502/503/504) | The ALB itself could not get a good response from a target | Step 2 |
| `HTTPCode_Target_5XX_Count` | nginx/the app returned a 5xx | Step 3 |

## 2. ELB-generated 5xx

- **503** - usually no healthy targets. Check target health:

  ```bash
  TG_ARN=$(aws elbv2 describe-target-groups --region "$REGION" --names "aws-lab-$ENV-tg" \
    --query 'TargetGroups[0].TargetGroupArn' --output text)
  aws elbv2 describe-target-health --region "$REGION" --target-group-arn "$TG_ARN" \
    --query 'TargetHealthDescriptions[].[Target.Id,TargetHealth.State,TargetHealth.Reason]' --output table
  ```

  If all targets are unhealthy, follow [asg-unhealthy-instances.md](asg-unhealthy-instances.md).

- **502** - target closed the connection or sent a malformed response. Usually
  nginx crashed or is restarting. Connect to an instance (Step 3) and check
  `systemctl status nginx`.
- **504** - target did not respond within the ALB idle timeout (60s). Look at
  the "Target response time" widget and instance CPU. If CPU is pinned, the
  ASG may already be at `MaxSize` - see Step 4.

## 3. Target-generated 5xx

Open a shell on an affected instance with Session Manager (no SSH or bastion):

```bash
aws ssm start-session --region "$REGION" --target <instance-id>
sudo systemctl status nginx
sudo tail -n 100 /var/log/nginx/error.log
sudo journalctl -u nginx --since "30 min ago"
df -h && free -m
```

Common causes: a bad deployment/config change (`sudo nginx -t`), full disk,
or memory exhaustion.

## 4. Mitigate

- **Bad instance(s):** terminate them and let the ASG replace them:
  `aws autoscaling terminate-instance-in-auto-scaling-group --instance-id <id> --no-should-decrement-desired-capacity`
- **Capacity:** temporarily raise capacity:
  `aws autoscaling update-auto-scaling-group --auto-scaling-group-name aws-lab-$ENV-web-asg --max-size 6 --desired-capacity 4`
- **Bad change:** roll back by redeploying the previous template/launch template version with `scripts/deploy.sh`.

## 5. Close out

- Confirm the alarm returns to `OK` and the error rate is back to baseline.
- Record timeline, root cause and follow-up actions in the incident ticket.
