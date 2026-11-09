#!/usr/bin/env bash
# Build + deploy both Cloud Run services and wire Pub/Sub push. YOU run this.
# Usage: PROJECT_ID=my-antarctica REGION=us-central1 ./infra/deploy.sh
# Optional: FRONTEND_ORIGIN=https://antarctica.vercel.app lets that browser origin call
# the API (CORS). Several origins: FRONTEND_ORIGIN="https://a.vercel.app,https://b.dev"
#
# Before deploying anything this runs the test suite and checks that every
# migration in db/migrations/ has been applied to DATABASE_URL. Set DATABASE_URL
# for that check; SKIP_PREFLIGHT=1 bypasses both, which you should need roughly
# never.
set -euo pipefail
: "${PROJECT_ID:?set PROJECT_ID}"
REGION="${REGION:-us-central1}"
REPO="${REGION}-docker.pkg.dev/${PROJECT_ID}/antarctica"
TAG="$(git rev-parse --short HEAD 2>/dev/null || date +%s)"
API_SA="antarctica-api@${PROJECT_ID}.iam.gserviceaccount.com"
SCAN_SA="antarctica-scanner@${PROJECT_ID}.iam.gserviceaccount.com"
PUSH_SA="antarctica-pusher@${PROJECT_ID}.iam.gserviceaccount.com"
ENV="PROJECT_ID=${PROJECT_ID},REGION=${REGION}"

# CORS origins for the API. gcloud splits --set-env-vars on commas, so a value that
# itself contains commas ("a,b") would be cut in two. `gcloud topic escaping`: start
# the flag value with ^@^ and gcloud splits on "@" instead, leaving commas intact.
API_ENV="${ENV}"
if [[ -n "${FRONTEND_ORIGIN:-}" ]]; then
  if [[ "${FRONTEND_ORIGIN}" == *@* ]]; then
    echo "FRONTEND_ORIGIN must not contain '@'" >&2; exit 1
  fi
  API_ENV="^@^PROJECT_ID=${PROJECT_ID}@REGION=${REGION}@CORS_ORIGINS=${FRONTEND_ORIGIN}"
else
  echo "note: FRONTEND_ORIGIN not set; the API will only allow CORS from http://localhost:5173"
fi

# ---------------------------------------------------------------- preflight --
# Nothing below is reversible in under a minute, so the cheap checks run first.
# Skipping either of these is what turned a one-line schema error into an hour of
# outage: set SKIP_PREFLIGHT=1 only when you know exactly why.
# Which interpreter runs the preflight. macOS ships no `python` at all, only
# `python3`, so a bare `python` here failed with "command not found" before a
# single test ran -- and because that is a non-zero exit like any other, the
# script reported "tests failed, refusing to deploy" and stopped. Deploys were
# refused for weeks on that machine while the message said the tests were bad.
# $PYTHON lets a virtualenv or a pinned interpreter override it.
PYTHON="${PYTHON:-$(command -v python3 || command -v python)}"
if [[ -z "$PYTHON" ]]; then
  echo "deploy: no python3 on PATH; set PYTHON=/path/to/python" >&2; exit 1
fi

if [[ "${SKIP_PREFLIGHT:-0}" != "1" ]]; then
  echo "== Preflight: tests ($PYTHON)"
  if ! "$PYTHON" -m pytest -q; then
    echo "deploy: tests failed, refusing to deploy. SKIP_PREFLIGHT=1 overrides." >&2
    exit 1
  fi

  echo "== Preflight: database schema matches this checkout"
  # Exit 2 (cannot check) is also a stop. A deploy you could not verify is exactly
  # the one that caused the incident this check exists for.
  if ! "$PYTHON" infra/preflight.py; then
    echo "deploy: schema check failed, refusing to deploy. SKIP_PREFLIGHT=1 overrides." >&2
    exit 1
  fi
else
  echo "note: SKIP_PREFLIGHT=1, deploying without running tests or checking the schema"
fi

echo "== Build images with Cloud Build (no local Docker needed)"
# CHROMIUM_VERSION pins the browser in the scanner image (see services/scanner/
# Dockerfile). Unset means "whatever Debian ships today": fine for a first build,
# but set it once you know the good version so a rebuild cannot move scores.
gcloud builds submit --config=infra/cloudbuild.yaml \
  --substitutions="_REPO=${REPO},_TAG=${TAG},_CHROMIUM_VERSION=${CHROMIUM_VERSION:-}" .

echo "== Deploy scanner (private: only Pub/Sub may call it)"
gcloud run deploy antarctica-scanner --image="${REPO}/scanner:${TAG}" --region="$REGION" \
  --service-account="$SCAN_SA" --no-allow-unauthenticated \
  --concurrency=1 --cpu=2 --memory=2Gi --timeout=300 --min-instances=0 --max-instances=3 \
  --set-env-vars="${ENV}" --set-secrets="DATABASE_URL=database-url:latest"
SCANNER_URL="$(gcloud run services describe antarctica-scanner --region="$REGION" \
  --format='value(status.url)')"
gcloud run services add-iam-policy-binding antarctica-scanner --region="$REGION" \
  --member="serviceAccount:${PUSH_SA}" --role=roles/run.invoker >/dev/null

echo "== Deploy API (public)"
gcloud run deploy antarctica-api --image="${REPO}/api:${TAG}" --region="$REGION" \
  --service-account="$API_SA" --allow-unauthenticated --min-instances=0 --max-instances=3 \
  --set-env-vars="${API_ENV}" --set-secrets="DATABASE_URL=database-url:latest"

echo "== Push subscription: Pub/Sub -> scanner, 5 attempts, then dead-letter"
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
PUBSUB_SA="service-${PROJECT_NUMBER}@gcp-sa-pubsub.iam.gserviceaccount.com"
gcloud pubsub topics add-iam-policy-binding scans-dlq \
  --member="serviceAccount:${PUBSUB_SA}" --role=roles/pubsub.publisher >/dev/null
if gcloud pubsub subscriptions describe scans-push >/dev/null 2>&1; then
  gcloud pubsub subscriptions update scans-push --push-endpoint="${SCANNER_URL}/pubsub/push" \
    --push-auth-service-account="$PUSH_SA"
else
  gcloud pubsub subscriptions create scans-push --topic=scans \
    --push-endpoint="${SCANNER_URL}/pubsub/push" --push-auth-service-account="$PUSH_SA" \
    --ack-deadline=300 --dead-letter-topic=scans-dlq --max-delivery-attempts=5 \
    --min-retry-delay=10s --max-retry-delay=300s
fi
gcloud pubsub subscriptions add-iam-policy-binding scans-push \
  --member="serviceAccount:${PUBSUB_SA}" --role=roles/pubsub.subscriber >/dev/null

API_URL="$(gcloud run services describe antarctica-api --region="$REGION" --format='value(status.url)')"
echo
echo "API: ${API_URL}"
echo "Try: curl -X POST ${API_URL}/scans -H 'content-type: application/json' -d '{\"url\":\"https://example.com\"}'"
