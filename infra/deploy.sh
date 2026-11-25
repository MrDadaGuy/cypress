#!/usr/bin/env bash
# Build and deploy Cypress. Infrastructure itself is Terraform (infra/terraform).
#   infra/deploy.sh          # API + ingest job + admin UI
#   infra/deploy.sh api      # API + ingest job only
#   infra/deploy.sh web      # admin UI only
#   infra/deploy.sh tickets  # Rust tickets service only
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT=lhca-cypress
REGION=us-central1
what=${1:-all}

if [[ $what == all || $what == api ]]; then
  tag="$(git rev-parse --short HEAD)$(git diff --quiet HEAD -- services/agent || echo -dirty)-$(date +%m%d%H%M)"
  img="$REGION-docker.pkg.dev/$PROJECT/cypress/agent:$tag"
  gcloud builds submit services/agent --project $PROJECT --region $REGION --tag "$img" --quiet
  gcloud run services update cypress-api --image "$img" --region $REGION --project $PROJECT --quiet
  gcloud run jobs update cypress-ingest --image "$img" --region $REGION --project $PROJECT --quiet
  url=$(gcloud run services describe cypress-api --region $REGION --project $PROJECT --format='value(status.url)')
  curl -sf "$url/health" >/dev/null && echo "API healthy: $url"
fi

if [[ $what == all || $what == tickets ]]; then
  tag="$(git rev-parse --short HEAD)$(git diff --quiet HEAD -- services/tickets || echo -dirty)-$(date +%m%d%H%M)"
  img="$REGION-docker.pkg.dev/$PROJECT/cypress/tickets:$tag"
  gcloud builds submit services/tickets --project $PROJECT --region $REGION --tag "$img" --quiet
  gcloud run services update cypress-tickets --image "$img" --region $REGION --project $PROJECT --quiet
  echo "Tickets deployed: $img"
fi

if [[ $what == all || $what == web ]]; then
  (cd apps/web && VITE_RELEASE=$(git rev-parse --short HEAD) npm run build && firebase deploy --only hosting --project $PROJECT --non-interactive)
fi
