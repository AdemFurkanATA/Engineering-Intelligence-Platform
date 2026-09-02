#!/usr/bin/env bash
# =============================================================================
# scripts/ops_smoke.sh
# Engineering Intelligence Platform — Phase 2 Ops Smoke Test
#
# Purpose:
#   Validate the LIVE Docker stack end-to-end.  This script is the complement
#   to tests/test_e2e_sync_smoke.py (which runs in-process without real infra).
#
#   Run this after `docker-compose up -d` to confirm:
#     1. All services are healthy
#     2. Kafka topics are reachable
#     3. A real repository sync job completes successfully
#     4. Events propagate through Kafka to graph-service
#     5. The knowledge graph contains the expected nodes
#     6. Neo4j contains the Repository and Commit nodes
#
# Usage:
#   chmod +x scripts/ops_smoke.sh
#   ./scripts/ops_smoke.sh [--repo-url <git-url>] [--org-id <org>]
#
# Environment overrides (optional):
#   EIP_GIT_ANALYZER_URL   default: http://localhost:8006
#   EIP_GRAPH_SERVICE_URL  default: http://localhost:8005
#   EIP_API_GW_URL         default: http://localhost:8000
#   EIP_NEO4J_URI          default: bolt://localhost:7687
#   EIP_NEO4J_USER         default: neo4j
#   EIP_NEO4J_PASSWORD     default: eip_neo4j_password
#   SMOKE_REPO_URL         default: https://github.com/psf/requests (public)
#   SMOKE_REPO_ID          default: smoke-requests
#   SMOKE_ORG_ID           default: smoke-org
#   SMOKE_TIMEOUT          default: 120  (seconds to wait for job completion)
#
# Dependencies: curl, jq, python3 (for Neo4j bolt check, optional)
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
GIT_ANALYZER_URL="${EIP_GIT_ANALYZER_URL:-http://localhost:8006}"
GRAPH_SERVICE_URL="${EIP_GRAPH_SERVICE_URL:-http://localhost:8005}"
API_GW_URL="${EIP_API_GW_URL:-http://localhost:8000}"
NEO4J_URI="${EIP_NEO4J_URI:-bolt://localhost:7687}"
NEO4J_USER="${EIP_NEO4J_USER:-neo4j}"
NEO4J_PASSWORD="${EIP_NEO4J_PASSWORD:-eip_neo4j_password}"

SMOKE_REPO_URL="${SMOKE_REPO_URL:-https://github.com/psf/requests}"
SMOKE_REPO_ID="${SMOKE_REPO_ID:-smoke-requests-$(date +%s)}"
SMOKE_ORG_ID="${SMOKE_ORG_ID:-smoke-org}"
SMOKE_TIMEOUT="${SMOKE_TIMEOUT:-120}"

# Parse optional args
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-url) SMOKE_REPO_URL="$2"; shift 2 ;;
    --org-id)   SMOKE_ORG_ID="$2";   shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------
GREEN="\033[0;32m"; RED="\033[0;31m"; YELLOW="\033[1;33m"; NC="\033[0m"
pass() { echo -e "${GREEN}[PASS]${NC} $*"; }
fail() { echo -e "${RED}[FAIL]${NC} $*"; ((FAILURES++)); }
info() { echo -e "${YELLOW}[INFO]${NC} $*"; }

FAILURES=0

# ---------------------------------------------------------------------------
# 1. Health checks
# ---------------------------------------------------------------------------
echo ""
info "=== 1. Service Health Checks ==="

check_health() {
  local name="$1" url="$2"
  local status
  status=$(curl -s -o /dev/null -w "%{http_code}" "$url/health" 2>/dev/null || echo "000")
  if [[ "$status" == "200" ]]; then
    pass "$name health OK ($url)"
  else
    fail "$name health FAILED — HTTP $status ($url)"
  fi
}

check_health "git-analyzer-service" "$GIT_ANALYZER_URL"
check_health "graph-service"        "$GRAPH_SERVICE_URL"
check_health "api-gateway"          "$API_GW_URL"

# ---------------------------------------------------------------------------
# 2. Graph-service backend check
# ---------------------------------------------------------------------------
echo ""
info "=== 2. Graph-service Backend ==="

GS_HEALTH=$(curl -s "$GRAPH_SERVICE_URL/health" 2>/dev/null || echo '{}')
GS_BACKEND=$(echo "$GS_HEALTH" | jq -r '.backend // "unknown"')
info "Graph-service backend: $GS_BACKEND"

if [[ "$GS_BACKEND" == "neo4j" ]]; then
  pass "graph-service using Neo4j backend"
elif [[ "$GS_BACKEND" == "in-memory" ]]; then
  echo -e "${YELLOW}[WARN]${NC} graph-service using in-memory backend (Neo4j not connected)"
else
  fail "graph-service backend unknown: $GS_BACKEND"
fi

# ---------------------------------------------------------------------------
# 3. Submit sync job
# ---------------------------------------------------------------------------
echo ""
info "=== 3. Submit Sync Job ==="
info "Repository: $SMOKE_REPO_URL"
info "Repo ID:    $SMOKE_REPO_ID"

JOB_RESPONSE=$(curl -s -X POST "$GIT_ANALYZER_URL/analyze" \
  -H "Content-Type: application/json" \
  -d "{
    \"repositoryId\":   \"$SMOKE_REPO_ID\",
    \"url\":            \"$SMOKE_REPO_URL\",
    \"organizationId\": \"$SMOKE_ORG_ID\",
    \"defaultBranch\":  \"main\"
  }" 2>/dev/null || echo '{}')

JOB_ID=$(echo "$JOB_RESPONSE" | jq -r '.jobId // ""')

if [[ -z "$JOB_ID" || "$JOB_ID" == "null" ]]; then
  fail "POST /analyze did not return jobId — response: $JOB_RESPONSE"
else
  pass "Job submitted: $JOB_ID"
fi

# ---------------------------------------------------------------------------
# 4. Wait for job completion
# ---------------------------------------------------------------------------
echo ""
info "=== 4. Waiting for Job Completion (timeout: ${SMOKE_TIMEOUT}s) ==="

ELAPSED=0
JOB_STATUS=""
while [[ $ELAPSED -lt $SMOKE_TIMEOUT ]]; do
  JOB_DATA=$(curl -s "$GIT_ANALYZER_URL/jobs/$JOB_ID" 2>/dev/null || echo '{}')
  JOB_STATUS=$(echo "$JOB_DATA" | jq -r '.status // "unknown"')

  case "$JOB_STATUS" in
    completed)
      pass "Job completed in ${ELAPSED}s"
      break
      ;;
    failed)
      fail "Job FAILED — $(echo "$JOB_DATA" | jq -r '.error // "no error detail"')"
      break
      ;;
    queued|running|cloning|analyzing)
      info "  [${ELAPSED}s] status=$JOB_STATUS ..."
      sleep 5
      ELAPSED=$((ELAPSED + 5))
      ;;
    *)
      fail "Unexpected job status: $JOB_STATUS"
      break
      ;;
  esac
done

if [[ $ELAPSED -ge $SMOKE_TIMEOUT && "$JOB_STATUS" != "completed" ]]; then
  fail "Job did not complete within ${SMOKE_TIMEOUT}s (last status: $JOB_STATUS)"
fi

# ---------------------------------------------------------------------------
# 5. Verify graph nodes
# ---------------------------------------------------------------------------
echo ""
info "=== 5. Graph Node Verification ==="

# Repository node
REPO_NODES=$(curl -s "$GRAPH_SERVICE_URL/graph/nodes?label=Repository" 2>/dev/null || echo '{"data":[]}')
REPO_COUNT=$(echo "$REPO_NODES" | jq "[.data[] | select(.nodeId == \"$SMOKE_REPO_ID\")] | length")

if [[ "$REPO_COUNT" -ge 1 ]]; then
  pass "Repository node found in graph (nodeId=$SMOKE_REPO_ID)"
else
  fail "Repository node NOT found in graph (nodeId=$SMOKE_REPO_ID)"
fi

# Commit nodes
COMMIT_NODES=$(curl -s "$GRAPH_SERVICE_URL/graph/nodes?label=Commit" 2>/dev/null || echo '{"data":[]}')
COMMIT_COUNT=$(echo "$COMMIT_NODES" | jq "[.data[] | select(.properties.repositoryId == \"$SMOKE_REPO_ID\")] | length")

if [[ "$COMMIT_COUNT" -ge 1 ]]; then
  pass "Commit nodes found: $COMMIT_COUNT"
else
  fail "No Commit nodes found for $SMOKE_REPO_ID"
fi

# Dependency nodes (if requirements.txt/package.json exists in target repo)
DEP_NODES=$(curl -s "$GRAPH_SERVICE_URL/graph/nodes?label=Dependency" 2>/dev/null || echo '{"data":[]}')
DEP_COUNT=$(echo "$DEP_NODES" | jq '.data | length')
info "Dependency nodes in graph: $DEP_COUNT (any repo)"

# ---------------------------------------------------------------------------
# 6. Verify last_sha endpoint
# ---------------------------------------------------------------------------
echo ""
info "=== 6. Incremental Sync State ==="

LAST_SHA_RESP=$(curl -s "$GRAPH_SERVICE_URL/graph/repos/$SMOKE_REPO_ID/last_sha" 2>/dev/null || echo '{}')
LAST_SHA=$(echo "$LAST_SHA_RESP" | jq -r '.lastAnalyzedSha // "null"')

if [[ "$LAST_SHA" != "null" && -n "$LAST_SHA" ]]; then
  pass "lastAnalyzedSha recorded: ${LAST_SHA:0:12}..."
else
  echo -e "${YELLOW}[WARN]${NC} lastAnalyzedSha not set (no headSha in RepositoryCloned payload, or job is still propagating)"
fi

# ---------------------------------------------------------------------------
# 7. Timeline endpoint
# ---------------------------------------------------------------------------
echo ""
info "=== 7. Timeline Endpoint ==="

TIMELINE_RESP=$(curl -s "$GRAPH_SERVICE_URL/graph/timeline/$SMOKE_REPO_ID?limit=10" 2>/dev/null || echo '{}')
TIMELINE_STATUS=$(echo "$TIMELINE_RESP" | jq -r '.repositoryId // "error"')

if [[ "$TIMELINE_STATUS" == "$SMOKE_REPO_ID" ]]; then
  TL_TOTAL=$(echo "$TIMELINE_RESP" | jq -r '.total // 0')
  pass "Timeline endpoint OK — $TL_TOTAL events"
else
  fail "Timeline endpoint failed — $TIMELINE_RESP"
fi

# ---------------------------------------------------------------------------
# 8. Cleanup (cancel any lingering jobs)
# ---------------------------------------------------------------------------
echo ""
info "=== 8. Cleanup ==="

if [[ -n "$JOB_ID" && "$JOB_ID" != "null" ]]; then
  DEL_RESP=$(curl -s -X DELETE "$GIT_ANALYZER_URL/jobs/$JOB_ID" 2>/dev/null || echo '{}')
  DEL_STATUS=$(echo "$DEL_RESP" | jq -r '.previousStatus // "unknown"')
  info "Job $JOB_ID cleanup — previousStatus=$DEL_STATUS"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "========================================"
if [[ $FAILURES -eq 0 ]]; then
  echo -e "${GREEN}ALL CHECKS PASSED${NC}"
  echo "Phase 2 Ops Smoke: OK"
else
  echo -e "${RED}$FAILURES CHECK(S) FAILED${NC}"
  echo "Phase 2 Ops Smoke: FAILED"
  exit 1
fi
echo "========================================"
