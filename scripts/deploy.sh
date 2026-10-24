#!/usr/bin/env bash
#
# Deploy the aws-lab CloudFormation stacks in dependency order:
#   network -> app -> monitoring -> automation
#
# Usage: scripts/deploy.sh -e <env> -r <region> [options]
# Run with --help for the full list of options.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CFN_DIR="${REPO_ROOT}/cloudformation"
LAMBDA_DIR="${REPO_ROOT}/lambda"

PROJECT="aws-lab"
ENVIRONMENT=""
REGION=""
OWNER="platform-team"
COST_CENTER="CC-1001"
ALERT_EMAIL=""
ENABLE_NAT="true"
STACKS="network app monitoring automation"

usage() {
  cat <<USAGE
Usage: $(basename "$0") -e <env> -r <region> [options]

Deploys the aws-lab stacks with 'aws cloudformation deploy'.

Required:
  -e, --env <env>            Environment name: dev | staging | prod
  -r, --region <region>      AWS region, e.g. ap-south-1

Options:
  -p, --project <name>       Project name prefix (default: ${PROJECT})
      --owner <owner>        Owner tag value (default: ${OWNER})
      --cost-center <cc>     CostCenter tag value (default: ${COST_CENTER})
      --alert-email <email>  Email subscribed to the alert topic (optional)
      --no-nat               Do not create a NAT Gateway (saves cost; private
                             instances lose general internet access)
      --only "<stacks>"      Space-separated subset to deploy, in order
                             (default: "${STACKS}")
  -h, --help                 Show this help and exit

Examples:
  $(basename "$0") -e dev -r ap-south-1 --alert-email ops@example.com
  $(basename "$0") -e dev -r ap-south-1 --only "network app"
USAGE
}

log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    -e|--env)        ENVIRONMENT="${2:-}"; shift 2 ;;
    -r|--region)     REGION="${2:-}"; shift 2 ;;
    -p|--project)    PROJECT="${2:-}"; shift 2 ;;
    --owner)         OWNER="${2:-}"; shift 2 ;;
    --cost-center)   COST_CENTER="${2:-}"; shift 2 ;;
    --alert-email)   ALERT_EMAIL="${2:-}"; shift 2 ;;
    --no-nat)        ENABLE_NAT="false"; shift ;;
    --only)          STACKS="${2:-}"; shift 2 ;;
    -h|--help)       usage; exit 0 ;;
    *)               usage >&2; die "Unknown argument: $1" ;;
  esac
done

[[ -n "${ENVIRONMENT}" ]] || { usage >&2; die "--env is required"; }
[[ -n "${REGION}" ]]      || { usage >&2; die "--region is required"; }
case "${ENVIRONMENT}" in dev|staging|prod) ;; *) die "--env must be dev, staging or prod" ;; esac

command -v aws >/dev/null 2>&1 || die "AWS CLI v2 is required"
command -v python3 >/dev/null 2>&1 || die "python3 is required to package the Lambda functions"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text --region "${REGION}")"
log "Account ${ACCOUNT_ID}, region ${REGION}, project ${PROJECT}, env ${ENVIRONMENT}"

TAGS=(
  "Project=${PROJECT}"
  "Environment=${ENVIRONMENT}"
  "Owner=${OWNER}"
  "CostCenter=${COST_CENTER}"
)
COMMON_PARAMS=(
  "ProjectName=${PROJECT}"
  "Environment=${ENVIRONMENT}"
  "Owner=${OWNER}"
  "CostCenter=${COST_CENTER}"
)

stack_name() { printf '%s-%s-%s' "${PROJECT}" "${ENVIRONMENT}" "$1"; }

deploy_stack() {
  local component="$1"; shift
  local name
  name="$(stack_name "${component}")"
  log "Deploying stack ${name} from ${component}.yaml"
  aws cloudformation deploy \
    --region "${REGION}" \
    --stack-name "${name}" \
    --template-file "${CFN_DIR}/${component}.yaml" \
    --capabilities CAPABILITY_IAM \
    --no-fail-on-empty-changeset \
    --tags "${TAGS[@]}" \
    --parameter-overrides "${COMMON_PARAMS[@]}" "$@"
}

# Zip one Lambda function directory and upload it to the artifact bucket.
# The S3 key contains a content hash so CloudFormation sees code changes.
package_lambda() {
  local fn="$1" bucket="$2" workdir="$3"
  local zip_path="${workdir}/${fn}.zip"
  (cd "${LAMBDA_DIR}/${fn}" && python3 -m zipfile -c "${zip_path}" handler.py)
  local hash
  hash="$(sha256sum "${zip_path}" | cut -c1-12)"
  local key="lambda/${fn}-${hash}.zip"
  aws s3 cp --only-show-errors --region "${REGION}" "${zip_path}" "s3://${bucket}/${key}"
  printf '%s' "${key}"
}

ensure_artifact_bucket() {
  local bucket="$1"
  if aws s3api head-bucket --bucket "${bucket}" --region "${REGION}" >/dev/null 2>&1; then
    return 0
  fi
  log "Creating artifact bucket ${bucket}"
  if [[ "${REGION}" == "us-east-1" ]]; then
    aws s3api create-bucket --bucket "${bucket}" --region "${REGION}" >/dev/null
  else
    aws s3api create-bucket --bucket "${bucket}" --region "${REGION}" \
      --create-bucket-configuration "LocationConstraint=${REGION}" >/dev/null
  fi
  aws s3api put-public-access-block --bucket "${bucket}" --region "${REGION}" \
    --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
  aws s3api put-bucket-encryption --bucket "${bucket}" --region "${REGION}" \
    --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
  aws s3api put-bucket-tagging --bucket "${bucket}" --region "${REGION}" \
    --tagging "TagSet=[{Key=Project,Value=${PROJECT}},{Key=Environment,Value=${ENVIRONMENT}},{Key=Owner,Value=${OWNER}},{Key=CostCenter,Value=${COST_CENTER}}]"
}

for component in ${STACKS}; do
  case "${component}" in
    network)
      deploy_stack network "EnableNatGateway=${ENABLE_NAT}"
      ;;
    app)
      deploy_stack app
      ;;
    monitoring)
      deploy_stack monitoring "AlertEmail=${ALERT_EMAIL}"
      ;;
    automation)
      bucket="${PROJECT}-${ENVIRONMENT}-artifacts-${ACCOUNT_ID}-${REGION}"
      ensure_artifact_bucket "${bucket}"
      workdir="$(mktemp -d)"
      trap 'rm -rf "${workdir}"' EXIT
      scheduler_key="$(package_lambda ec2_scheduler "${bucket}" "${workdir}")"
      report_key="$(package_lambda cost_hygiene_report "${bucket}" "${workdir}")"
      deploy_stack automation \
        "CodeBucket=${bucket}" \
        "SchedulerCodeKey=${scheduler_key}" \
        "CostReportCodeKey=${report_key}"
      ;;
    *)
      die "Unknown stack component: ${component}"
      ;;
  esac
done

if [[ " ${STACKS} " == *" app "* ]]; then
  url="$(aws cloudformation describe-stacks --region "${REGION}" \
    --stack-name "$(stack_name app)" \
    --query "Stacks[0].Outputs[?OutputKey=='LoadBalancerUrl'].OutputValue" --output text)"
  log "Application URL: ${url}"
fi

log "Done. Remember: NAT Gateway and ALB bill hourly - run scripts/destroy.sh when finished."
