#!/usr/bin/env bash
# Publish Till for PayPal (PayPal AI Hackathon build) to GitHub. Run ONLY after Von approves.
#
#   bash scripts/publish.sh                  dry run: checks + prints every command, changes nothing
#   bash scripts/publish.sh --run private    step 1: push to ydescieux2-del/till-paypal (PRIVATE), push, set About
#   bash scripts/publish.sh --run public     step 2 (separate Von approval): flip the repo PUBLIC
#
# Both steps run the pre-push checks first and abort on any finding. If the auto-mode classifier blocks the
# push, Von runs the same line himself as a ! command, e.g.  !bash ~/till-paypal/scripts/publish.sh --run private
set -euo pipefail

REPO="ydescieux2-del/till-paypal"
DESC="Voice CFO for PayPal sellers: ask how your money is doing, then preview and confirm invoices, refunds and dispute settlements. PayPal REST APIs, Claude, deterministic cash engine, MCP server, MIT."
TOPICS=(paypal paypal-api agentic-commerce claude mcp model-context-protocol voice-agent small-business ag-grid hackathon python)
DEMO_URL="${DEMO_URL:-}"   # set when hosting exists: DEMO_URL=https://... bash scripts/publish.sh --run private

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

RUN=0; STEP="all"
for a in "$@"; do
  case "$a" in
    --run) RUN=1 ;;
    private|public) STEP="$a" ;;
    *) echo "usage: bash scripts/publish.sh [--run] [private|public]"; exit 2 ;;
  esac
done
if [[ $RUN == 1 && $STEP == all ]]; then echo "ABORT: --run needs a step: private or public"; exit 2; fi

say() { printf '\n== %s\n' "$*"; }
run() {
  printf '  $ %s\n' "$*"
  if [[ $RUN == 1 ]]; then "$@"; fi
}
die() { echo "ABORT: $*"; exit 1; }

checks() {
  say "pre-push checks"
  [[ "$(pwd)" == "$ROOT" && -d .git ]] || die "not in the till-paypal repo root"
  [[ -z "$(git status --porcelain)" ]] || die "working tree not clean; commit or stash first"

  for f in .env notes data/sandbox_captures.json data/dispute_orders.json; do
    git check-ignore -q "$f" || die ".gitignore does not cover $f"
    [[ -z "$(git ls-files -- "$f")" ]] || die "$f is tracked by git; remove it from the index"
  done
  [[ -z "$(git log --all --format=%H -- .env notes data/sandbox_captures.json data/dispute_orders.json | head -1)" ]] \
    || die "an ignored local file appears in git history"
  echo "  ok  .gitignore covers .env, notes/, sandbox order logs; none tracked or in history"

  grep -q "MIT License" LICENSE || die "LICENSE is not MIT"
  echo "  ok  LICENSE is MIT"

  # Key patterns, over every tracked file AND every commit in history.
  local pat='sk-ant-[A-Za-z0-9_-]{10,}|tvly-[A-Za-z0-9_-]{16,}|sk-[A-Za-z0-9]{32,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|(PAYPAL_CLIENT_SECRET|ELEVENLABS_API_KEY|OPENAI_API_KEY|ANTHROPIC_API_KEY) *= *[A-Za-z0-9_-]{8,}'
  if git grep -nIE "$pat" -- . ':!scripts/publish.sh' >/dev/null; then
    git grep -nIE "$pat" -- . ':!scripts/publish.sh' | cut -c1-120
    die "key-like string in tracked files"
  fi
  if git log --all -p -- . ':!scripts/publish.sh' | grep -IEq "$pat"; then
    die "key-like string in git history"
  fi
  # The actual key values from .env / environment, if present, must not appear anywhere tracked or in history.
  local vals=()
  for k in PAYPAL_CLIENT_ID PAYPAL_CLIENT_SECRET ANTHROPIC_API_KEY ELEVENLABS_API_KEY; do
    v="${!k:-}"
    [[ -z "$v" && -f .env ]] && v="$(grep -E "^$k=" .env | head -1 | cut -d= -f2- | tr -d '"' || true)"
    [[ ${#v} -ge 8 ]] && vals+=("$v")
  done
  for v in "${vals[@]+"${vals[@]}"}"; do
    git grep -qF -- "$v" && die "a real key value from .env is in a tracked file"
    git log --all -p | grep -qF -- "$v" && die "a real key value from .env is in git history"
  done
  echo "  ok  secret scan clean (patterns + ${#vals[@]} live key value(s), tracked files + full history)"

  command -v gh >/dev/null || die "gh not installed"
  gh auth status >/dev/null 2>&1 || die "gh not logged in"
  echo "  ok  gh logged in"
}

step_private() {
  say "STEP 1: create $REPO PRIVATE and push (needs Von's approval)"
  if gh repo view "$REPO" >/dev/null 2>&1; then
    echo "  note: $REPO already exists; skipping create, pushing to it"
    git remote get-url origin >/dev/null 2>&1 || run git remote add origin "https://github.com/$REPO.git"
    run git push -u origin main
  else
    run gh repo create "$REPO" --private --source=. --remote=origin --push --description "$DESC"
  fi
  local topic_args=()
  for t in "${TOPICS[@]}"; do topic_args+=(--add-topic "$t"); done
  run gh repo edit "$REPO" --description "$DESC" "${topic_args[@]}"
  if [[ -n "$DEMO_URL" ]]; then
    run gh repo edit "$REPO" --homepage "$DEMO_URL"
    run gh variable set DEMO_URL --repo "$REPO" --body "$DEMO_URL"   # turns on the keep-warm workflow
  else
    echo "  (DEMO_URL unset: rerun with DEMO_URL=https://... to set the About website link and keep-warm ping)"
  fi
  run gh repo view "$REPO" --json visibility,licenseInfo,description,repositoryTopics,homepageUrl
  echo "  GitHub reads LICENSE and shows MIT in About automatically."
}

step_public() {
  say "STEP 2: flip $REPO PUBLIC (SEPARATE Von approval; judges need it public before Nov 12 12:00 pm PT)"
  run gh repo edit "$REPO" --visibility public --accept-visibility-change-consequences
  run gh repo view "$REPO" --json visibility,licenseInfo,url
}

checks
[[ $RUN == 0 ]] && say "DRY RUN: nothing below is executed"
case "$STEP" in
  all)     step_private; step_public ;;
  private) step_private ;;
  public)  step_public ;;
esac
say "done ($([[ $RUN == 1 ]] && echo executed || echo dry run))"
