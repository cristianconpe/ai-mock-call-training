# AI Mock Call Training

An internal training tool where new Microsoft Support engineers practice live voice calls with an AI-simulated customer, then get an objective, KB-grounded evaluation of the call.

The first scenario (`DP-001`) covers the M365 Data Protection — Global Admin MFA Reset process (Standard Verification, POV hierarchy, security hold, etc.).

## How it works

- **Customer agent**: GPT roleplays a difficult-but-realistic customer, grounded only in the scenario's persona/case data — it never has access to the KB, so it can't leak internal process knowledge.
- **Voice loop**: browser mic (push-to-talk) → Whisper transcription → GPT customer reply → TTS-HD audio played back in the browser.
- **Timer**: the call's time budget only counts actual speaking time (customer audio playing, trainee recording) — not processing time in between.
- **Evaluator**: a separate GPT call grades the trainee against the scenario's KB (required steps, forbidden actions) with evidence quotes; the numeric score is computed deterministically in code so scoring stays consistent across trainees. Pronunciation is graded using Whisper's real transcription-confidence signal, not just clean-looking text.
- **"Call back after 24h holding time" button**: simulates the required security-hold follow-up call without literally waiting 24 hours.

## Project structure

```
scenarios/data_protection/dp_001.json   # scenario data — persona, case, KB, evaluation criteria
static/                                 # web UI (index.html, call.js, styles.css)
web_app.py                              # FastAPI + WebSocket backend (customer agent, evaluator, scoring)
text_test.py                            # text-only CLI harness for the customer agent (no audio)
voice_test.py                           # voice-only CLI harness (mic + speaker, no browser)
```

## Setup

Requires Python 3.11+ and an Azure AI Foundry resource with GPT, Whisper, and TTS-HD deployments.

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
cp .env.example .env   # then fill in your real Azure endpoint/key values
```

`.env` is gitignored — never commit real credentials.

## Run

```bash
.venv\Scripts\python.exe -m uvicorn web_app:app --reload --port 8000 --ws-ping-timeout 120
```

Open `http://localhost:8000` in a real browser (mic access is blocked in sandboxed/embedded previews). The extended `--ws-ping-timeout` keeps the socket alive through the end-of-call evaluation request, which can take longer than the 20s default.
