"""Run the watchdog's actual shell blocks with deterministic API/time fixtures."""

import json
import os
import shlex
import subprocess

import pytest

from test_workflow_notifications import notification_script


def check_health(tmp_path, conclusions, scheduled_count=12, last_scheduled=True):
    runs = {"total_count": len(conclusions), "workflow_runs": [
        {"conclusion": conclusion} for conclusion in conclusions
    ]}
    last = {"workflow_runs": [{"created_at": "2026-10-12T23:00:00Z"}] if last_scheduled else []}
    prelude = f'''
gh() {{
  case "$*" in
    *'created=>'*) printf '%s\\n' '{json.dumps({"total_count": scheduled_count, "workflow_runs": []})}' ;;
    *'event=schedule'*) printf '%s\\n' '{json.dumps(last)}' ;;
    *'runs?per_page='*) printf '%s\\n' '{json.dumps(runs)}' ;;
    *) printf '%s\\n' '{{"state":"active"}}' ;;
  esac
}}
date() {{
  case "$*" in
    *'24 hours ago'*) echo 2026-10-12T00:00:00Z ;;
    *'-d '*) echo {1791676800 if scheduled_count == 0 else 1791846000} ;;
    *) echo 1791849600 ;;
  esac
}}
'''
    output = tmp_path / "outputs"
    env = dict(os.environ, GITHUB_OUTPUT=str(output), MONITORED_WORKFLOW="check-coin-news.yml",
               STALE_THRESHOLD_MINUTES="360", EXPECTED_SCHEDULED_RUNS_PER_DAY="12",
               CONSECUTIVE_FAILURES_TO_ALERT="3")
    result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", prelude +
                             notification_script("watchdog.yml", "Check monitored workflow health")],
                            env=env, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    return dict(line.split("=", 1) for line in output.read_text().splitlines())


@pytest.mark.parametrize("conclusions,last_scheduled", [([], False), (["success"], False), (["success"], True)])
def test_zero_scheduled_runs_sends_explicit_telegram_alert(tmp_path, conclusions, last_scheduled):
    outputs = check_health(tmp_path, conclusions, scheduled_count=0, last_scheduled=last_scheduled)
    assert outputs["alert"] == "true"
    assert outputs.get("reason") == "0_runs"
    assert "0 runs" in outputs["message"]
    # Exercise the alert transport using the health output, without network/secrets.
    prelude = f'''
exec 3>&1
TELEGRAM_RECIPIENTS=synthetic:chat
ALERT_MESSAGE={shlex.quote(outputs["message"])}
curl() {{
  for arg in "$@"; do
    case "$arg" in text=*) printf '%s\\n' "${{arg#text=}}" >&3 ;; esac
  done
}}
'''
    sent = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", prelude +
                           notification_script("watchdog.yml", "Send Telegram alert")],
                          capture_output=True, text=True, timeout=5)
    assert sent.returncode == 0, sent.stderr
    assert sent.stdout.strip() == outputs["message"]


@pytest.mark.parametrize("conclusion", ["success", "cancelled", "skipped", "neutral", "timed_out", "action_required", "stale", "startup_failure", "unknown"])
def test_non_failure_conclusions_break_failure_streak(tmp_path, conclusion):
    outputs = check_health(tmp_path, ["failure", conclusion, "failure"])
    assert outputs["alert"] == "false"


@pytest.mark.parametrize("conclusions,alert", [
    (["failure", "failure", "failure"], "true"),
    (["failure", "failure"], "false"),
    ([None, "failure", "failure"], "false"),
])
def test_only_completed_failures_reach_threshold(tmp_path, conclusions, alert):
    assert check_health(tmp_path, conclusions)["alert"] == alert
