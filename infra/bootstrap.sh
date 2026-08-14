#!/usr/bin/env bash
# Bring up a Frappe v15 bench with ERPNext and ai_bookkeeper installed.
#
#   bash infra/bootstrap.sh
#
# Re-runnable: every step is skipped if it has already been done. The first run
# is slow -- it clones frappe and erpnext and builds a venv and assets.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DC="docker compose -f $HERE/docker-compose.yml"
SITE="${SITE:-dev.localhost}"
BENCH_ROOT=/home/frappe/bench
BENCH=$BENCH_ROOT/frappe-bench
PY=/usr/bin/python3.11   # Frappe v15 does not support the image's default 3.14

fr() { $DC exec -T frappe bash -lc "$1"; }
be() { fr "cd $BENCH && $1"; }

echo "==> services"
$DC up -d
$DC ps --format '  {{.Service}}\t{{.Status}}'

echo "==> bench init (slow on first run)"
# Test for sites/apps.txt, which bench init writes near the end. Two weaker
# checks are tempting and both wrong: the venv directory appears seconds after
# init starts, and `import frappe` succeeds as soon as pip finishes -- while
# init carries on with the yarn install and asset build for several more
# minutes. Either one makes a re-run skip a still-running init and then fail on
# the next command.
if ! fr "test -f $BENCH/sites/apps.txt"; then
  fr "mkdir -p $BENCH_ROOT && cd $BENCH_ROOT && bench init \
        --frappe-branch version-15 \
        --python $PY \
        --skip-redis-config-generation \
        frappe-bench"
else
  echo "    already initialised"
fi

echo "==> point bench at the compose services"
be "bench set-config -g db_host mariadb"
be "bench set-config -g redis_cache redis://redis-cache:6379"
be "bench set-config -g redis_queue redis://redis-queue:6379"
be "bench set-config -g redis_socketio redis://redis-queue:6379"

echo "==> site"
if ! fr "test -d $BENCH/sites/$SITE"; then
  be "bench new-site $SITE \
        --db-root-password 123 \
        --admin-password admin \
        --no-mariadb-socket"
else
  echo "    site $SITE already exists"
fi

echo "==> erpnext"
if ! fr "test -d $BENCH/apps/erpnext"; then
  be "bench get-app --branch version-15 erpnext"
fi
be "bench --site $SITE install-app erpnext" || echo "    (already installed)"

echo "==> ai_bookkeeper (symlinked from the repo, so edits are live)"
fr "ln -sfn /workspace/apps/ai_bookkeeper $BENCH/apps/ai_bookkeeper"
# bench writes apps.txt without a trailing newline, so a naive append yields
# "erpnextai_bookkeeper" on one line and every later command dies importing it.
be "sed -i -e '\$a\\' sites/apps.txt"
be "grep -qx ai_bookkeeper sites/apps.txt || echo ai_bookkeeper >> sites/apps.txt"
be "sed -i '/^\$/d' sites/apps.txt"
be "env/bin/pip install --quiet -e apps/ai_bookkeeper"
be "bench --site $SITE install-app ai_bookkeeper" || echo "    (already installed)"
be "bench --site $SITE set-config developer_mode 1"

echo "==> company + chart of accounts"
be "bench --site $SITE execute ai_bookkeeper.setup.bootstrap.ensure_company"

echo "==> import the 15-row sample and categorise it"
be "bench --site $SITE execute ai_bookkeeper.setup.bootstrap.import_sample"

cat <<EOF

  ------------------------------------------------------------------
  Start the server:
    $DC exec frappe bash -lc "cd $BENCH && bench serve --port 8000"

  Then open:  http://$SITE:8000      Administrator / admin

  Use the site hostname, not plain localhost. Frappe resolves which site to
  serve from the Host header, so http://localhost:8000 returns a 404.
  Browsers route *.localhost to 127.0.0.1 with no setup needed.
  ------------------------------------------------------------------

  Start here:
    /app/ai-bookkeeper          workspace, with live counts
    /app/staged-bank-transaction?workflow_state=Pending%20Review
    /app/ai-categorization-log  what the AI proposed vs what was decided

  Tests:
    $DC exec -T frappe bash -lc "cd $BENCH && bench --site $SITE run-tests --app ai_bookkeeper \
      --module ai_bookkeeper.ai_bookkeeper.doctype.staged_bank_transaction.test_staged_bank_transaction"

  Reset and re-import the demo data:
    $DC exec -T frappe bash -lc "cd $BENCH && bench --site $SITE execute ai_bookkeeper.setup.bootstrap.reset_demo"
    $DC exec -T frappe bash -lc "cd $BENCH && bench --site $SITE execute ai_bookkeeper.setup.bootstrap.import_sample"

EOF
