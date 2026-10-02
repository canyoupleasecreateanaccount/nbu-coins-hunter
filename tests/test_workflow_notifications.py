"""Execute notification blocks with synthetic recipients and controlled curl outcomes."""

from pathlib import Path
import re
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
STEPS = [
    ("check-coin-news.yml", "Notify on failure"),
    ("watchdog.yml", "Send Telegram alert"),
    ("watchdog.yml", "Notify on watchdog failure"),
]


def notification_script(workflow, name):
    source = (ROOT / ".github" / "workflows" / workflow).read_text()
    step = source.split(f"      - name: {name}\n", 1)[1]
    block = step.split("        run: |\n", 1)[1]
    lines = []
    for line in block.splitlines():
        if line and not line.startswith("          "):
            break
        lines.append(line[10:])
    return re.sub(r"\$\{\{.*?\}\}", "synthetic", "\n".join(lines))


@pytest.mark.parametrize("workflow,name", STEPS)
@pytest.mark.parametrize("outcome", [200, 401, 429, 503, "timeout"])
@pytest.mark.parametrize("recipients", ["synthetic:first,synthetic:second,synthetic:third", "synthetic:first\nsynthetic:second\nsynthetic:third", ""])
def test_notification_outcomes_and_recipient_continuation(workflow, name, outcome, recipients):
    script = notification_script(workflow, name)
    # A shell function replaces only transport; the actual workflow runs with
    # GitHub Actions' errexit and pipefail settings. No network or secrets used.
    prelude = r'''
curl() {
  local arg chat=""
  for arg in "$@"; do
    case "$arg" in chat_id=*) chat="${arg#chat_id=}" ;; esac
  done
  echo "attempt:${chat}" >&3
  echo '{"result":{"chat":{"id":"synthetic-private-chat"}}}'
  echo "https://api.telegram.org/botsynthetic-private-token/sendMessage" >&2
  if [ "$chat" = first ]; then
    return SYNTHETIC_STATUS
  fi
  return 0
}
TELEGRAM_RECIPIENTS='SYNTHETIC_RECIPIENTS'
TELEGRAM_BOT_TOKEN=synthetic
TELEGRAM_CHAT_ID=first
ALERT_MESSAGE=synthetic
'''
    status = 0 if outcome == 200 else 28 if outcome == "timeout" else 22
    prelude = prelude.replace("SYNTHETIC_STATUS", str(status)).replace("SYNTHETIC_RECIPIENTS", recipients)
    result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", "exec 3>&1\n" + prelude + script], capture_output=True, text=True, timeout=5)
    expected = ["attempt:first", "attempt:second", "attempt:third"] if recipients else ["attempt:first"]
    assert [line for line in result.stdout.splitlines() if line.startswith("attempt:")] == expected
    assert (result.returncode == 0) == (outcome == 200)
    assert "synthetic-private" not in result.stdout + result.stderr
    assert "api.telegram.org" not in result.stdout + result.stderr


@pytest.mark.parametrize("workflow,name", STEPS)
def test_notification_curl_limits(workflow, name):
    curl_call = notification_script(workflow, name).split("curl ", 1)[1].split("done", 1)[0]
    assert "--fail" in curl_call
    assert "--connect-timeout 10" in curl_call
    assert "--max-time 30" in curl_call
