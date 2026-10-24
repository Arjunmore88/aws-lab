#!/usr/bin/env bash
#
# Delete the aws-lab CloudFormation stacks in reverse dependency order:
#   automation -> monitoring -> app -> network
#
# Usage: scripts/destroy.sh -e <env> -r <region> [--yes] [--delete-artifacts]
# Run with --help for the full list of options.

set -euo pipefail

PROJECT="aws-lab"
ENVIRONMENT=""
REGION=""
ASSUME_YES="false"
DELETE_ARTIFACTS="false"
STACKS="automation monitoring app network"

usage() {
  cat <<USAGE
Usage: $(basename "$0") -e <env> -r <region> [options]

Deletes the aws-lab stacks in reverse order and waits for each deletion.

Required:
  -e, --env <env>          Environment name: dev | staging | prod
  -r, --region <region>    AWS region the stacks were deployed to

Options:
  -p, --project <name>     Project name prefix (default: ${PROJECT})
  -y, --yes                Do not prompt for confirmation
      --delete-artifacts   Also empty and delete the Lambda artifact bucket
  -h, --help               Show this help and exit

Example:
  $(basename "$0") -e dev -r ap-south-1 --yes --delete-artifacts
USAGE
}

log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    -e|--env)           ENVIRONMENT="${2:-}"; shift 2 ;;
    -r|--region)        REGION="${2:-}"; shift 2 ;;
    -p|--project)       PROJECT="${2:-}"; shift 2 ;;
    -y|--yes)           ASSUME_YES="true"; shift ;;
    --delete-artifacts) DELETE_ARTIFACTS="true"; shift ;;
    -h|--help)          usage; exit 0 ;;
    *)                  usage >&2; die "Unknown argument: $1" ;;
  esac
done

[[ -n "${ENVIRONMENT}" ]] || { usage >&2; die "--env is required"; }
[[ -n "${REGION}" ]]      || { usage >&2; die "--region is required"; }
command -v aws >/dev/null 2>&1 || die "AWS CLI v2 is required"

if [[ "${ASSUME_YES}" != "true" ]]; then
  read -r -p "Delete all ${PROJECT}-${ENVIRONMENT}-* stacks in ${REGION}? Type the environment name to confirm: " answer
  [[ "${answer}" == "${ENVIRONMENT}" ]] || die "Confirmation did not match - aborting"
fi

for component in ${STACKS}; do
  name="${PROJECT}-${ENVIRONMENT}-${component}"
  if ! aws cloudformation describe-stacks --region "${REGION}" --stack-name "${name}" >/dev/null 2>&1; then
    log "Stack ${name} not found - skipping"
    continue
  fi
  log "Deleting stack ${name}"
  aws cloudformation delete-stack --region "${REGION}" --stack-name "${name}"
  aws cloudformation wait stack-delete-complete --region "${REGION}" --stack-name "${name}"
  log "Deleted ${name}"
done

if [[ "${DELETE_ARTIFACTS}" == "true" ]]; then
  account_id="$(aws sts get-caller-identity --query Account --output text --region "${REGION}")"
  bucket="${PROJECT}-${ENVIRONMENT}-artifacts-${account_id}-${REGION}"
  if aws s3api head-bucket --bucket "${bucket}" --region "${REGION}" >/dev/null 2>&1; then
    log "Emptying and deleting artifact bucket ${bucket}"
    aws s3 rb "s3://${bucket}" --force --region "${REGION}"
  else
    log "Artifact bucket ${bucket} not found - skipping"
  fi
fi

log "Teardown complete. Check the Billing console over the next day to confirm charges have stopped."
