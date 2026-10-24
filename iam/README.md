# IAM policy examples

Example customer-managed policies written for the aws-lab scenario. They follow
the same rules used for the Lambda and instance roles in `cloudformation/`:
grant the narrowest set of actions, scope resources by ARN or tag wherever the
service supports it, and use explicit `Deny` statements for guardrails.

| File | Attach to | What it allows |
|------|-----------|----------------|
| [`developer-readonly-ssm.json`](developer-readonly-ssm.json) | Developer group | Read-only visibility into EC2, ALB, ASG, CloudWatch, Logs and CloudFormation, plus SSM Session Manager shell access **only** to instances tagged `Project=aws-lab` and `Environment=dev`. No SSH keys or port 22 needed. |
| [`s3-bucket-scoped-access.json`](s3-bucket-scoped-access.json) | Application role | List, read, write and delete objects under the `uploads/` prefix of a single bucket. Denies any request that is not over TLS. |
| [`deny-outside-approved-regions.json`](deny-outside-approved-regions.json) | Group/permission boundary or SCP | Guardrail that denies regional actions outside `ap-south-1` and `us-east-1`. Global services (IAM, STS, Billing, Route 53, CloudFront) are excluded via `NotAction`. |

## Design notes

- **Describe actions use `"Resource": "*"`.** Most `Describe*`/`List*` APIs do
  not support resource-level permissions, so scoping them is not possible.
  Mutating actions are always scoped.
- **Tag-based access (ABAC).** `ssm:StartSession` is conditioned on
  `aws:ResourceTag/...`, so new instances are covered automatically as long as
  they are tagged by the launch template. Tagging permissions themselves should
  be restricted so users cannot re-tag an instance to gain access.
- **Sessions are owner-scoped.** `TerminateSession`/`ResumeSession` use
  `${aws:username}` so a developer can only end their own sessions.
- **Placeholders.** Replace `example-company-app-assets`, the region list, and
  tag values before use.

## Validating

Use IAM Access Analyzer policy validation before attaching a policy:

```bash
aws accessanalyzer validate-policy \
  --policy-type IDENTITY_POLICY \
  --policy-document file://iam/developer-readonly-ssm.json
```
