# GUI user test (agentic, pixel-only)

`.github/workflows/gui-user-test.yml` runs an LLM as a first-time user of the
dashboard: it sees only screenshots and acts only through the mouse and keyboard,
against a real Chromium window on a private Xvfb display and a real gateway seeded
from a fixture. It exists for the defects the DOM-level lanes cannot see -- a control
the DOM calls visible that a person cannot click, a theme switch that changed a class
name and not the pixels, a horizontal scrollbar, a spinner that never stops. Tracking
issue: [#9578](https://github.com/kirodotdev/KiroCrew/issues/9578).

The lane is **advisory**. On a pull request it upserts one comment and never turns
the check red; only a `workflow_dispatch` or the nightly run reports the real outcome
as the job status. It is not a required status check and `pr-readiness.yml` does not
read it.

## Pieces

| Path | Role |
|---|---|
| `.github/workflows/gui-user-test.yml` | Triggers, boot, run, artifact, PR comment, nightly issue, lane status. |
| `scripts/gui-user-test/boot.sh` | Xvfb -> `seed_home.py` -> `python -m kiro_crew gateway --test-mode --approval yolo --no-crons` on the packaged fake ACP backend -> Chromium at the dashboard URL. Writes `target.env` (origin + one-time token, mode 0600) and `pids`. |
| `scripts/gui-user-test/seed_home.py` | Copies a fixture into `$KIROCREW_HOME` through `kiro_crew.seed` and adds `config.agents.<slug>` for each `--member` so the Crew Members page has a roster. |
| `scripts/gui-user-test/teardown.sh` | Kills the three process groups and removes the scratch home and browser profile. |
| `test/gui_user/harness.py` | The screenshot -> Bedrock Messages API -> action loop with the step, time and budget gates. |
| `test/gui_user/x11.py` | Screenshots (Pillow `ImageGrab`) and input (`xdotool`); coordinate scaling, key aliases and argv building are pure and unit-tested. |
| `test/gui_user/scenarios.py` + `scenarios/*.yaml` | The scenario DSL and the shipped scenarios. |
| `test/gui_user/report.py` | Renders `summary.json` into `verdict.md`, the PR comment and the nightly issue. |

The unit tests under `test/gui_user/` run in the ordinary Backend Tests shards; they
need no display and never call Bedrock.

## How a run works

1. The runner installs `xvfb`, `xdotool`, `x11-utils`, the backend (`pip install -e .`
   plus `boto3`), builds the frontend and stages it into `src/kiro_crew/static/dist`
   exactly as the E2E job does.
2. `boot.sh` starts Xvfb `:99` at 1600x1000, seeds a throwaway `KIROCREW_HOME`, starts
   the gateway with `KIROCREW_KIRO_BIN` pointed at
   `kiro_crew.testing.fake_acp_backend` (so chat replies without kiro-cli or a
   login), reads the `KIROCREW_READY:{port, token}` line, opens Chromium
   (`--no-sandbox --test-type`, full-screen window, omnibox kept) at
   `http://127.0.0.1:<port>/?token=...`, focuses the window.
3. `harness.py` navigates to each scenario's `start_url` through the omnibox (the
   token has become the `mc_token` cookie by then), takes a screenshot, and loops:
   the model returns one action, the harness executes it, waits about a second, and
   returns a fresh screenshot as the tool result. The conversation keeps the newest
   three screenshots; older ones become a one-line placeholder.
4. The model ends with a `VERDICT: PASS|FAIL` block listing each expectation as
   `MET` / `NOT MET` plus any UI defects it noticed. A scenario that fails is retried
   once; `max_steps` / `max_seconds` from the YAML and the run's `--budget-usd` are
   hard stops.
5. Everything lands in the `gui-user-test-<run id>` artifact: `results/<scenario>/attempt-N/NN-<action>.png`,
   `steps.jsonl` (every action with parameters and the screenshot it produced),
   `summary.json`, `verdict.md`, plus `gateway.log` / `gateway.err` / `chrome.log` /
   `xvfb.log` with the one-time token scrubbed.

### The model and the tool shape

The harness speaks the Bedrock Messages API through `boto3` under the same OIDC role
the review lanes use (`secrets.AWS_BEDROCK_ROLE_ARN`, `us-west-2`). It first offers
the native computer-use tool (`computer_20251124` under the
`computer-use-2025-11-24` beta); if the model or endpoint rejects the beta with a 400
it switches, for the rest of the run, to a plain tool-use loop with one custom tool
per action (`screenshot`, `left_click`, `double_click`, `right_click`, `mouse_move`,
`left_click_drag`, `type`, `key`, `scroll`, `wait`) and the screenshot returned as an
image block inside `tool_result`. Both shapes drive the same `x11.perform`, so the
logs and the scenarios are identical either way. `--tool-mode native|custom` pins one.

The model id is a workflow parameter (`inputs.model`, default
`us.anthropic.claude-sonnet-4-6`), never a default in code.

## Adding a scenario

Create `test/gui_user/scenarios/<name>.yaml`; the file stem must equal `name`:

```yaml
name: settings-theme-toggle
tier: smoke                 # smoke = runs on PRs and nightly; nightly = nightly only
summary: Switch the dashboard theme in Settings and confirm the colours change
preconditions:
  seed: rich                # KIROCREW_HOME fixture (kirocrew gateway --seed NAME)
  members: []               # crew member slugs boot.sh adds to config.agents
  start_url: /settings      # path the harness navigates to first (no query string)
steps:
  - You are on the Settings page. Take note of the current background colour.
  - Find the theme control and pick a different theme than the one selected.
expectations:
  - The page background colour is clearly different from the first screenshot.
max_steps: 12               # actions before the scenario FAILS (ceiling 40)
max_seconds: 300            # wall clock before the scenario FAILS (ceiling 900)
```

Write `steps` as you would brief a human tester -- what to look for, not where to
click -- and `expectations` as things that are true or false on the final screen. Keep
a scenario to one flow; the cheapest scenario is the one that needs the fewest
screenshots. `test_scenarios_and_report.py` loads every shipped file, so a malformed
scenario fails the unit tests before it costs a model call.

`boot.sh` seeds one home per run from `GUI_SEED` (default `rich`) with `GUI_MEMBERS`
(default `nova-sky`); a scenario's `preconditions.seed` / `members` document what it
needs and must agree with that boot, because the target is booted once per run.

## Running it

- **On a PR**: add the `gui-test` label. The lane runs the `smoke` tier on the PR's
  build and upserts its comment; remove and re-add the label to re-run after a push
  (a `synchronize` event also re-runs it while the label is present).
- **On demand**: `gh workflow run gui-user-test.yml --ref <branch> -f tier=nightly`
  (also `-f scenario=<name>`, `-f model=<id>`, `-f budget_usd=<n>`). A dispatch run's
  job status is the real verdict.
- **Nightly**: `20 9 * * *` UTC on `main`, full tier. A non-PASS night opens or
  updates the single open issue labelled `gui-test-report`.

### Locally

The harness needs an X display, `xdotool`, a Chromium, and AWS credentials for a
Bedrock role. On a machine with those:

```bash
sudo apt-get install -y xvfb xdotool x11-utils           # Debian/Ubuntu
pip install -e . "boto3>=1.34,<2"
(cd website && npm ci && npm run build) && rm -rf src/kiro_crew/static/dist && cp -R website/dist src/kiro_crew/static/dist
export GUI_OUT="$(mktemp -d)"
bash scripts/gui-user-test/boot.sh                       # Xvfb :99, gateway, Chromium
. "$GUI_OUT/target.env"
python test/gui_user/harness.py --out "$GUI_OUT/results" --base-url "$GUI_BASE_URL" \
  --model us.anthropic.claude-sonnet-4-6 --tier smoke --budget-usd 2
bash scripts/gui-user-test/teardown.sh
```

`--dry-run` skips the model entirely and only navigates + screenshots each scenario's
start page -- the cheapest check that the target booted and the display works. To
watch the run, point a VNC server at `:99` (`x11vnc -display :99`). Never point
`--display` at your own desktop: the backend refuses `:0` / `:1`.

## Cost and limits

- A 1280x800 screenshot is about 1 365 input tokens (width x height / 750). With three
  screenshots kept, a step costs roughly 6-8k input and ~150 output tokens; a
  10-step scenario on a Sonnet-class model is about $0.25-0.40 and two to four
  minutes. The nightly tier (three scenarios, one retry each in the worst case) is
  about $1-2.50; the PR smoke pair about $0.60. The run stops at `--budget-usd`
  (dispatch default $3, nightly $4, PR $2) and marks the remaining scenarios
  `SKIPPED`.
- Pixel tests are stochastic. One retry absorbs a mis-click; a scenario that flips
  night to night is a scenario problem (vague step, timing) before it is a product
  problem. Read `steps.jsonl` and the numbered screenshots: they show exactly where
  the model looked and what it did.
- The model never sees the DOM, so it cannot assert what a human cannot see either.
  Use the Playwright E2E suite for exact text and state; use this lane for "does it
  look and behave right to a person".
- Actions the native tool can emit but the backend refuses (`zoom`, `hold_key`,
  mouse down/up as separate events) come back as structured tool errors; the model
  recovers with the supported vocabulary.

## Security boundary

The display is a private Xvfb (`-nolisten tcp`); `x11.refuse_real_display` rejects
`:0` / `:1` so the lane can never drive a person's desktop. The model gets screenshot,
click, type, key, scroll and wait -- no shell, no file access, no clipboard -- and
typed text is capped at 400 characters. The system prompt declares on-screen text to
be data, never instructions. The target holds nothing worth stealing: a seeded
fixture, the fake backend, and a one-time token for a gateway that dies with the job
(scrubbed from the uploaded logs). The checkout uses `persist-credentials: false`,
and the Bedrock role is the review lanes' existing least-privilege role. Fork pull
requests never run the lane.
