"""
web_app.py

Same customer agent / Azure setup as text_test.py and voice_test.py, now
served over a small local web UI instead of the terminal: FastAPI +
WebSocket backend, browser mic (push-to-talk) for input, and WAV audio
played back in the browser for the customer's voice.

Confirmed working Azure setup (all on the hack2026win-4020-resource
Foundry resource, bare endpoint, no /api/projects/... or /openai/...
suffix in the .env values):
  - Chat completions api-version: 2024-10-21
  - Whisper transcription api-version: 2024-06-01
  - TTS speech api-version: 2024-12-01-preview (requested as WAV)

Run:
    .venv\\Scripts\\python.exe -m uvicorn web_app:app --reload --port 8000 --ws-ping-timeout 120
Then open http://localhost:8000 in a real browser (not a sandboxed preview).
The extended --ws-ping-timeout keeps the socket alive through a slow
end-of-call evaluation request, which can take longer than the 20s default.
"""

import asyncio
import io
import json
import os
import wave
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from openai import AzureOpenAI

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
STATIC_DIR = BASE_DIR / "static"
SCENARIOS_DIR = BASE_DIR / "scenarios"

# ==========================================
# AZURE CLIENTS
# ==========================================


def _require_env(name: str) -> str:
    # .strip() guards against stray whitespace/newlines that sneak in when
    # a value is copy-pasted into a Codespaces/CI secret field — a trailing
    # "\n" in a URL otherwise breaks httpx's URL parser with a confusing error.
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Copy .env.example to .env and fill in your real Azure AI Foundry values "
            f"(or set it as a Codespaces secret)."
        )
    return value


GPT_MODEL = os.environ.get("GPT_MODEL", "gpt-5-mini").strip()
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "whisper").strip()
TTS_MODEL = os.environ.get("TTS_MODEL", "tts-hd").strip()
TTS_VOICE = os.environ.get("TTS_VOICE", "alloy").strip()

gpt_client = AzureOpenAI(
    api_version="2024-10-21",
    azure_endpoint=_require_env("GPT_ENDPOINT"),
    api_key=_require_env("GPT_KEY"),
)

whisper_client = AzureOpenAI(
    api_version="2024-06-01",
    azure_endpoint=_require_env("WHISPER_ENDPOINT"),
    api_key=_require_env("WHISPER_KEY"),
)

tts_client = AzureOpenAI(
    api_version="2024-12-01-preview",
    azure_endpoint=_require_env("TTS_ENDPOINT"),
    api_key=_require_env("TTS_KEY"),
)

# ==========================================
# SCENARIO LOADING
# ==========================================

REQUIRED_TOP_LEVEL_KEYS = [
    "scenario_id",
    "area",
    "difficulty",
    "duration_minutes",
    "customer",
    "case",
    "knowledge_base",
    "evaluation",
]


def load_scenario(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        scenario = json.load(f)
    missing = [k for k in REQUIRED_TOP_LEVEL_KEYS if k not in scenario]
    if missing:
        raise ValueError(f"Scenario '{path}' is missing required keys: {missing}")
    return scenario


def get_customer_safe_view(scenario: dict) -> dict:
    return {"customer": scenario["customer"], "case": scenario["case"]}


def _load_all_scenarios() -> dict:
    scenarios = {}
    for path in SCENARIOS_DIR.rglob("*.json"):
        s = load_scenario(path)
        scenarios[s["scenario_id"]] = s
    return scenarios


SCENARIOS = _load_all_scenarios()

# ==========================================
# CUSTOMER PROMPT + AGENT
# ==========================================

CUSTOMER_PROMPT_TEMPLATE = """You are roleplaying as a CUSTOMER calling Microsoft Support. You are NOT an AI assistant, you are NOT a support agent, and you must never break character or offer troubleshooting advice.

# WHO YOU ARE
You are a real person with a real, frustrating problem. You called support because you need help — you are not here to be helpful to the support engineer, you are here to get your problem solved.

# ABSOLUTE RULES
1. NEVER use internal support/process terminology, even if the trainee uses it. Forbidden words/phrases include (but are not limited to): "POV", "Point of Verification", "SPV", "SV", "Standard Verification", "Entitlement", "Security Hold", "Assist 365", "GDAP", "diagnostic tool", "TA", "escalation". If the trainee uses these terms, you can ask them to explain in plain language, but you never use them yourself.
2. You do not know Microsoft's internal support process, workflow, or policies. You do not know what is "required" or "forbidden" for the agent to do. You just want your problem fixed.
3. NEVER reveal information listed under "Things you know but don't say unless asked" unless the trainee's question satisfies the stated reveal condition. If they ask something vague or unrelated, do not volunteer it — you can express mild confusion instead.
4. NEVER resolve your own case, NEVER suggest the correct process step, and NEVER refuse a request on policy grounds — you don't know the policy. If the trainee offers to do something (even something against policy), react as a normal customer would: usually relief or agreement, not suspicion.
5. Stay emotionally realistic per your persona and attitude below — professionally frustrated, impatient, occasionally confused, but NEVER abusive, NEVER insulting, NEVER a cartoonish or over-the-top character.
6. Your attitude can shift during the call: if the trainee is clear, competent, and explains things well, you soften. If they are vague, contradictory, or dismissive, you become more visibly (but still professionally) frustrated.
7. Speak naturally, like a real phone call — contractions, occasional interruptions of thought, realistic phrasing. Not a script being read aloud.
8. DO NOT STALL OR LOOP. Look back at the conversation so far: if you have already raised a specific objection, asked a specific question, or made a specific request, and the trainee has already addressed it — even briefly, even just once or twice — do NOT raise that exact same point again in any form, reworded or not. Accept it (with residual frustration if that fits your attitude) and move on. A real call always keeps moving forward; it never circles back to re-litigate the same point three, four, five times.
9. DO NOT MANUFACTURE NEW DEMANDS. This is the opposite failure mode from looping, and it is just as important: don't invent a growing list of new asks each turn (a case number, a supervisor's name, an exact timestamp, a callback window, an escalation) just to keep pushing. Real customers do not operate like a bureaucratic checklist. Only ask for something like that if (a) it's something you would realistically and specifically want given your actual goal, AND (b) you haven't already asked for it. Once the engineer gives you real forward progress, your instinct is relief and cooperation — not adding conditions. But de-escalating your TONE is not the same as ending the call — see the call_should_end rule below, a plan is not a resolution.
10. You have a small, finite number of things you actually care about (see your goal and pushback lines below) — not an unlimited supply of complaints. Once you've voiced your main concerns once, you're done raising new ones; from then on you're just responding to what the engineer does.

# OUTPUT FORMAT — MANDATORY
You must respond with ONLY a valid JSON object, no markdown fences, no extra text, in exactly this shape:

{{"customer_message": "<what you say out loud, in character>", "call_should_end": <true or false>, "end_reason": "<short reason, or null if false>"}}

Set "call_should_end": true ONLY in this exact situation: the engineer has explicitly asked you to confirm you can sign in now (or that your issue is fully resolved), AND you have confirmed yes. That is the ONLY way this call ends automatically otherwise (besides you personally needing to go, or the call running on for an unreasonable amount of time). Nothing else counts, no matter how good it sounds: NOT a completed verification ("both checks are confirmed"), NOT being told you can "proceed with the reset now", NOT even being told the reset itself is done ("I've reset your MFA") — none of that ends the call by itself. Only the explicit ask-and-confirm about actually signing in successfully does. If the engineer says anything short of that, stay engaged: react naturally to what they said, but keep call_should_end false. Otherwise, always false.

# SCENARIO FOR THIS CALL
{SCENARIO_BLOCK}
"""


class CustomerAgentError(Exception):
    pass


SOFTEN_REMINDER = (
    "[Reminder, not spoken aloud: you've now raised your main concern(s) about this. "
    "Do not repeat an objection or question you've already made, reworded or not — accept what "
    "the engineer has told you (even if reluctantly) and let the call keep moving.]"
)

CONVERGE_REMINDER = (
    "[Reminder, not spoken aloud: this call needs to actively move toward resolution now. "
    "IMPORTANT — if the engineer has explained a requirement, policy, or necessary wait (like a "
    "security hold) to you, you get exactly ONE round of pushing back on it ('that's not acceptable, "
    "can you do X instead?'). The very next time that SAME requirement comes up — even if you're "
    "still not happy about it — you accept it. Say something like 'okay, I understand, I'll wait for "
    "your call' and move on. Do not ask them to justify it again, do not repeat 'that's not "
    "acceptable', do not ask the same follow-up question about it a second time. Also do NOT "
    "introduce any new demands (a case number, a supervisor's name, an exact timestamp, "
    "a callback window, an escalation) that you haven't already asked for. Do NOT re-confirm or "
    "re-ask something you already asked earlier in this call, even if the engineer's answer to it "
    "wasn't perfectly complete — treat it as settled and move on. Cooperate with whatever the "
    "engineer does next. Remember: call_should_end only becomes true when they explicitly ask you to "
    "confirm sign-in success (or that your issue is fully resolved) and you confirm it — being told "
    "verification is done, or that they're proceeding with the reset, or even that the reset itself "
    "is done, does NOT end the call by itself.]"
)

# After this many trainee turns without the call ending, start nudging the
# customer toward acceptance instead of trusting the system prompt alone to
# keep enforcing it many turns later — a fresh reminder close to generation
# time is far more reliable than one rule buried in a long system prompt.
SOFTEN_AFTER_TURNS = 2
CONVERGE_AFTER_TURNS = 3


class CustomerAgent:
    def __init__(self, scenario: dict):
        self.scenario = scenario
        safe_view = get_customer_safe_view(scenario)
        scenario_block = json.dumps(safe_view, indent=2, ensure_ascii=False)
        system_prompt = CUSTOMER_PROMPT_TEMPLATE.replace("{SCENARIO_BLOCK}", scenario_block)
        self.history = [{"role": "system", "content": system_prompt}]
        self._opening_sent = False
        self._turns_since_progress = 0

    def get_opening_message(self) -> dict:
        opening_line = self.scenario["customer"]["opening_line"]
        turn = {"customer_message": opening_line, "call_should_end": False, "end_reason": None}
        self.history.append({"role": "assistant", "content": json.dumps(turn)})
        self._opening_sent = True
        return turn

    def _call_model(self) -> dict:
        response = gpt_client.chat.completions.create(
            model=GPT_MODEL,
            messages=self.history,
            response_format={"type": "json_object"},
        )
        raw_content = response.choices[0].message.content
        self.history.append({"role": "assistant", "content": raw_content})

        try:
            turn = json.loads(raw_content)
        except json.JSONDecodeError as e:
            raise CustomerAgentError(f"Model did not return valid JSON: {raw_content!r}") from e

        for key in ("customer_message", "call_should_end", "end_reason"):
            if key not in turn:
                raise CustomerAgentError(f"Model JSON missing key '{key}': {turn}")

        return turn

    def respond(self, trainee_message: str) -> dict:
        if not self._opening_sent:
            raise CustomerAgentError("Call get_opening_message() once before respond().")

        self.history.append({"role": "user", "content": trainee_message})
        self._turns_since_progress += 1

        if self._turns_since_progress >= CONVERGE_AFTER_TURNS:
            self.history.append({"role": "system", "content": CONVERGE_REMINDER})
        elif self._turns_since_progress >= SOFTEN_AFTER_TURNS:
            self.history.append({"role": "system", "content": SOFTEN_REMINDER})

        turn = self._call_model()
        return turn

    def advance_time(self, narrative_note: str) -> dict:
        """Injects a narrator-only note (not spoken by anyone) describing
        what changed while time passed, then asks the model for the
        customer's next line as a natural continuation/follow-up — used
        for the "call back after the security hold" jump instead of
        literally waiting 24 hours."""
        if not self._opening_sent:
            raise CustomerAgentError("Call get_opening_message() once before advance_time().")

        self.history.append(
            {
                "role": "system",
                "content": (
                    f"[TIME SKIP — narrator note, not visible to the customer or trainee as dialogue]: "
                    f"{narrative_note} Now speak your next line in character, as the customer picking "
                    f"the call back up after this time has passed. Keep it SHORT — 2-3 sentences, exactly "
                    f"like a normal turn in this call. Don't try to pack all of the background context "
                    f"into one line; just react naturally and let the rest come out over the next few "
                    f"turns as the engineer asks."
                ),
            }
        )
        # This is effectively a new call — reset the convergence counter so a
        # couple of fresh rounds of legitimate pushback are allowed again.
        self._turns_since_progress = 0
        turn = self._call_model()
        # This line is always the START of the next phase, never its end —
        # never let the model close the call right as it's picking back up,
        # no matter how the narrative note is worded. Also patch the stored
        # history entry so future turns don't see a stale call_should_end.
        turn["call_should_end"] = False
        turn["end_reason"] = None
        self.history[-1]["content"] = json.dumps(turn)
        return turn


# ==========================================
# AUDIO HELPERS
# ==========================================


def transcribe_webm(audio_bytes: bytes) -> tuple[str, float, Optional[float]]:
    """Returns (text, duration_s, avg_confidence). verbose_json gives us the
    actual audio duration plus per-segment avg_logprob — a real acoustic
    confidence signal (closer to 0 = more clearly transcribed) we use to
    ground pronunciation/intelligibility judgment in the evaluator instead
    of guessing from the (usually clean-looking) transcribed text alone."""
    buf = io.BytesIO(audio_bytes)
    buf.name = "trainee_turn.webm"
    response = whisper_client.audio.transcriptions.create(
        model=WHISPER_MODEL, file=buf, response_format="verbose_json"
    )
    text = (response.text or "").strip()
    duration = getattr(response, "duration", None) or 0.0
    segments = getattr(response, "segments", None) or []
    logprobs = [s.avg_logprob for s in segments if getattr(s, "avg_logprob", None) is not None]
    avg_confidence = sum(logprobs) / len(logprobs) if logprobs else None
    return text, duration, avg_confidence


def synthesize_wav(text: str, voice: str = TTS_VOICE) -> bytes:
    response = tts_client.audio.speech.create(
        model=TTS_MODEL, voice=voice, input=text, response_format="wav"
    )
    return response.read()


def wav_duration_s(wav_bytes: bytes) -> float:
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        return wf.getnframes() / float(wf.getframerate())


# ==========================================
# CALL SESSION
# ==========================================


class CallSession:
    """The 5-minute budget counts only actual speaking time (customer TTS
    audio playing + trainee mic audio recorded) — not the wall-clock time
    spent transcribing/thinking/synthesizing between turns, which would
    otherwise eat the whole budget on processing latency alone."""

    def __init__(self, scenario: dict):
        self.scenario = scenario
        self.agent = CustomerAgent(scenario)
        self.ended = False
        self.end_reason: Optional[str] = None
        self.transcript: list[dict] = []
        self.active_speaking_s = 0.0

    def duration_budget_s(self) -> float:
        return self.scenario["duration_minutes"] * 60

    def remaining_s(self) -> float:
        return max(0.0, self.duration_budget_s() - self.active_speaking_s)

    def is_time_up(self) -> bool:
        return self.active_speaking_s >= self.duration_budget_s()

    def add_speaking_time(self, seconds: float) -> None:
        self.active_speaking_s += max(0.0, seconds)

    def check_timeout(self) -> None:
        if not self.ended and self.is_time_up():
            self._end("timeout")

    def start(self) -> str:
        turn = self.agent.get_opening_message()
        self.transcript.append({"role": "customer", "text": turn["customer_message"]})
        if turn["call_should_end"]:
            self._end("natural_close")
        return turn["customer_message"]

    def handle_trainee_turn(
        self, text: str, duration_s: float, avg_confidence: Optional[float] = None
    ) -> Optional[str]:
        self.transcript.append({"role": "trainee", "text": text, "avg_confidence": avg_confidence})
        self.add_speaking_time(duration_s)
        if self.is_time_up():
            self._end("timeout")
            return None

        turn = self.agent.respond(text)
        self.transcript.append({"role": "customer", "text": turn["customer_message"]})
        if turn["call_should_end"]:
            self._end("natural_close")
        return turn["customer_message"]

    def advance_time(self) -> Optional[str]:
        """Triggers the scenario's time-skip (if it has one) — e.g. calling
        the customer back after the 24h security hold — without ending or
        pausing the call."""
        time_skip = self.scenario.get("case_progression", {}).get("time_skip")
        if not time_skip or self.ended:
            return None

        turn = self.agent.advance_time(time_skip["narrative_note"])
        self.transcript.append(
            {"role": "customer", "text": turn["customer_message"], "note": "time_skip"}
        )
        if turn["call_should_end"]:
            self._end("natural_close")
        return turn["customer_message"]

    def end_manually(self) -> None:
        if not self.ended:
            self._end("manual")

    def _end(self, reason: str) -> None:
        self.ended = True
        self.end_reason = reason


# ==========================================
# EVALUATOR
# ==========================================

EVALUATOR_PROMPT_TEMPLATE = """You are an objective call-quality evaluator for Microsoft Support training. You are grading a trainee (the "support engineer" speaker) on a mock call against the official process below. You are NOT the customer and you have no personality — you are a strict, fair auditor.

# Process being evaluated
{process_name}

# Required steps (the ONLY steps that count as mandatory)
{required_steps_bullets}

# Forbidden actions (the ONLY actions that count as rule violations, with their official severity)
{forbidden_actions_bullets}

# Escalation rules (for context only, informs whether escalation was handled correctly)
{escalation_rules_bullets}

# Critical rule about scope — read carefully
You may ONLY judge the trainee against the required steps and forbidden actions listed above. If something feels like it should be a rule but is not explicitly listed above, you must NOT treat it as required or forbidden — note it in "notes" as an observation only, never as a scored error. When in doubt, say you're uncertain rather than inventing a requirement. This transcript is the only evidence you have; do not assume facts not present in it.

# What to evaluate
1. For each required step listed above, decide whether the trainee satisfied it. Quote the transcript as evidence, or write "not observed" if it wasn't done.
2. For each forbidden action listed above, decide whether the trainee did it. If yes, report it using that action's exact id and severity, with a transcript quote as evidence. If it did not happen, do not report it.
3. Separately, note communication-quality issues (minor, not part of the KB above). These are never "critical" or "major" — only "minor". Tag each one with exactly one category:
   - "communication": tone, empathy, active listening, clarity, customer handling
   - "fluency": grammar, sentence structure, filler words, hesitation
   - "pronunciation": words that would genuinely be hard for a real customer to understand (never accent alone)
   - "call_management": efficiency, control of the conversation, pacing toward resolution
4. List concrete strengths (what the trainee did well) and improvements (specific, actionable) grounded in the transcript.

# Speech-to-text confidence data (real acoustic signal, not a guess)
{audio_metrics_summary}

# Fluency and pronunciation guardrail — extremely important
Do NOT penalize accent, ever — a Mexican, Indian, Brazilian, or any other accent is never on its own a pronunciation problem. That said, hold pronunciation to a genuinely high bar — the goal is for the trainee to reach their clearest, most confident English, not just "understandable enough". Use the confidence data above as real signal: it reflects how clearly the speech-to-text engine could make out the trainee's actual audio, independent of accent. Don't require a pattern across "several" turns before it counts — even one or two turns below the stated bar, or a single notably weak worst-turn score, is real evidence worth a minor pronunciation deduction. Don't wave it away just because the transcribed text reads cleanly (transcription engines often produce clean-looking text even from audio that was genuinely hard to make out — the confidence number is the more honest signal). Cross-check against the transcript too: repeated false starts, incomplete words, mid-sentence self-corrections, or run-on/mumbled phrasing are also valid evidence on their own, independent of the confidence numbers. The question is always "would a real customer have had trouble understanding this, or is this clearly polished, confident English" — not "does this sound non-native." Default to flagging genuine weaknesses rather than assuming a pass; this category should rarely max out unless the audio was genuinely clean and confident throughout.

# Full call transcript
{transcript}

# Output format
Respond with ONLY a JSON object with exactly these fields:
- "steps": array of {{"step_id": string (must match one of the required step ids above), "met": boolean, "evidence": string}}
- "critical_errors": array of {{"id": string (must match a forbidden action id above), "description": string, "evidence": string}} — only include ones that actually occurred
- "major_errors": array of {{"id": string (must match a forbidden action id above), "description": string, "evidence": string}} — only include ones that actually occurred
- "minor_errors": array of {{"id": string (a short slug you invent), "category": "communication" | "fluency" | "pronunciation" | "call_management", "description": string, "evidence": string}}
- "strengths": array of strings
- "improvements": array of strings
- "notes": string or null — any uncertainty, unspecified-rule observations, or caveats
"""


def _format_transcript_for_eval(transcript: list[dict]) -> str:
    lines = []
    for turn in transcript:
        speaker = "Customer" if turn["role"] == "customer" else "Support Engineer"
        lines.append(f"{speaker}: {turn['text']}")
    return "\n".join(lines)


# Rough heuristic: Whisper avg_logprob is usually roughly -0.1 to -0.3 for
# genuinely clear, confident speech. We hold a fairly high bar here — the
# goal is to push toward the trainee's best, clearest English, not just
# "good enough to be technically understood".
LOW_CONFIDENCE_THRESHOLD = -0.35


def _audio_metrics_summary(transcript: list[dict]) -> str:
    confidences = [
        t["avg_confidence"]
        for t in transcript
        if t.get("role") == "trainee" and t.get("avg_confidence") is not None
    ]
    if not confidences:
        return "No speech-to-text confidence data available for this call."

    avg = sum(confidences) / len(confidences)
    low_count = sum(1 for c in confidences if c < LOW_CONFIDENCE_THRESHOLD)
    worst = min(confidences)
    return (
        f"Trainee turns analyzed: {len(confidences)}. Average speech-to-text confidence: "
        f"{avg:.2f} (values near 0 = clearly transcribed; more negative = harder to make out). "
        f"Worst single-turn confidence: {worst:.2f}. "
        f"{low_count} of {len(confidences)} turns were below the {LOW_CONFIDENCE_THRESHOLD} bar for "
        f"genuinely clear speech."
    )


def evaluate_call(scenario: dict, transcript: list[dict]) -> dict:
    kb = scenario["knowledge_base"]
    required_steps_bullets = "\n".join(f"- {s['id']}: {s['text']}" for s in kb["required_steps"])
    forbidden_actions_bullets = "\n".join(
        f"- {a['id']} ({a['severity']}): {a['text']}" for a in kb["forbidden_actions"]
    )
    escalation_rules = kb.get("escalation_rules", [])
    escalation_rules_bullets = (
        "\n".join(f"- {r['id']}: {r['text']}" for r in escalation_rules)
        if escalation_rules
        else "- (none specified)"
    )

    prompt = EVALUATOR_PROMPT_TEMPLATE.format(
        process_name=scenario.get("process_name", scenario["area"]),
        required_steps_bullets=required_steps_bullets,
        forbidden_actions_bullets=forbidden_actions_bullets,
        escalation_rules_bullets=escalation_rules_bullets,
        audio_metrics_summary=_audio_metrics_summary(transcript),
        transcript=_format_transcript_for_eval(transcript),
    )

    response = gpt_client.chat.completions.create(
        model=GPT_MODEL,
        messages=[{"role": "system", "content": prompt}],
        response_format={"type": "json_object"},
    )
    findings = json.loads(response.choices[0].message.content)

    for key in ("steps", "critical_errors", "major_errors", "minor_errors", "strengths", "improvements"):
        findings.setdefault(key, [])
    findings.setdefault("notes", None)

    return findings


# ==========================================
# SCORING (deterministic — the LLM judges what happened, this computes
# the number, so scoring is consistent across trainees)
# ==========================================

PROCESS_KB_MAX = 45
COMMUNICATION_MAX = 20
FLUENCY_MAX = 15
PRONUNCIATION_MAX = 15
CALL_MANAGEMENT_MAX = 5

CRITICAL_PROCESS_CAP = 10
CRITICAL_OVERALL_CAP = 59
MAJOR_DEDUCTION = 8
MINOR_DEDUCTIONS = {"communication": 3, "fluency": 2, "pronunciation": 4, "call_management": 1}
TIMEOUT_CALL_MANAGEMENT_DEDUCTION = 1

_BANDS = [
    (95, 100, "Excellent"),
    (90, 94, "Very good"),
    (85, 89, "Good / Ready"),
    (75, 84, "Needs improvement"),
    (60, 74, "Significant improvement needed"),
    (0, 59, "Not ready"),
]

_RECOMMENDATIONS = {
    "Excellent": "Excellent performance — ready for real customer interactions.",
    "Very good": "Very good performance — ready with very minor polish.",
    "Good / Ready": "Good performance — ready with minor coaching opportunities.",
    "Needs improvement": "Needs improvement — repeat this scenario after addressing the notes below.",
    "Significant improvement needed": "Significant improvement needed before handling this scenario live.",
    "Not ready": "Not ready — a critical process rule was violated; review the KB and repeat this scenario.",
}


def _band_for(overall_score: int) -> str:
    for low, high, name in _BANDS:
        if low <= overall_score <= high:
            return name
    return "Not ready"


def _csat_stars(overall_score: int) -> int:
    if overall_score >= 85:
        return 5
    if overall_score >= 75:
        return 4
    if overall_score >= 65:
        return 3
    if overall_score >= 50:
        return 2
    return 1


def _clamp_round(value: float, low: float, high: float) -> int:
    return round(max(low, min(high, value)))


def score_call(scenario: dict, findings: dict, end_reason: Optional[str]) -> dict:
    kb = scenario["knowledge_base"]
    known_step_ids = {s["id"] for s in kb["required_steps"]}
    total_steps = len(known_step_ids) or 1
    met_steps = sum(1 for f in findings["steps"] if f.get("step_id") in known_step_ids and f.get("met"))

    forbidden_by_id = {a["id"]: a for a in kb["forbidden_actions"]}
    reported_ids = {e["id"] for e in findings["critical_errors"]} | {e["id"] for e in findings["major_errors"]}
    valid_critical_ids = {
        eid for eid in reported_ids if eid in forbidden_by_id and forbidden_by_id[eid]["severity"] == "critical"
    }
    valid_major_ids = {
        eid for eid in reported_ids if eid in forbidden_by_id and forbidden_by_id[eid]["severity"] == "major"
    }

    process_kb = PROCESS_KB_MAX * (met_steps / total_steps)
    process_kb -= MAJOR_DEDUCTION * len(valid_major_ids)
    process_kb = max(0.0, process_kb)
    if valid_critical_ids:
        process_kb = min(process_kb, CRITICAL_PROCESS_CAP)
    process_kb_score = _clamp_round(process_kb, 0, PROCESS_KB_MAX)

    minor_counts = {"communication": 0, "fluency": 0, "pronunciation": 0, "call_management": 0}
    for m in findings["minor_errors"]:
        cat = m.get("category")
        if cat in minor_counts:
            minor_counts[cat] += 1

    communication_score = _clamp_round(
        COMMUNICATION_MAX - MINOR_DEDUCTIONS["communication"] * minor_counts["communication"],
        0,
        COMMUNICATION_MAX,
    )
    fluency_score = _clamp_round(
        FLUENCY_MAX - MINOR_DEDUCTIONS["fluency"] * minor_counts["fluency"], 0, FLUENCY_MAX
    )
    pronunciation_score = _clamp_round(
        PRONUNCIATION_MAX - MINOR_DEDUCTIONS["pronunciation"] * minor_counts["pronunciation"],
        0,
        PRONUNCIATION_MAX,
    )

    call_management_raw = (
        CALL_MANAGEMENT_MAX - MINOR_DEDUCTIONS["call_management"] * minor_counts["call_management"]
    )
    if end_reason == "timeout":
        call_management_raw -= TIMEOUT_CALL_MANAGEMENT_DEDUCTION
    call_management_score = _clamp_round(call_management_raw, 0, CALL_MANAGEMENT_MAX)

    overall = (
        process_kb_score + communication_score + fluency_score + pronunciation_score + call_management_score
    )
    if valid_critical_ids:
        overall = min(overall, CRITICAL_OVERALL_CAP)
    overall = max(0, min(100, overall))

    band = _band_for(overall)

    return {
        "overall_score": overall,
        "category_scores": {
            "process_kb": process_kb_score,
            "communication": communication_score,
            "fluency": fluency_score,
            "pronunciation": pronunciation_score,
            "call_management": call_management_score,
        },
        "csat_stars": _csat_stars(overall),
        "band": band,
        "recommendation": _RECOMMENDATIONS[band],
        "findings": findings,
    }


# ==========================================
# FASTAPI APP
# ==========================================

app = FastAPI(title="AI Mock Call Training")
app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/scenarios")
def list_scenarios() -> list[dict]:
    result = []
    for s in SCENARIOS.values():
        time_skip = s.get("case_progression", {}).get("time_skip")
        result.append(
            {
                "scenario_id": s["scenario_id"],
                "area": s["area"],
                "process_name": s.get("process_name", s["area"]),
                "difficulty": s["difficulty"],
                "duration_minutes": s["duration_minutes"],
                "time_skip_label": time_skip["button_label"] if time_skip else None,
                "overview": s.get("case", {}).get("summary", ""),
                "goal": s.get("case", {}).get("customer_goal", ""),
            }
        )
    return result


async def _send_customer_line(websocket: WebSocket, session: CallSession, text: str) -> None:
    # remaining_s here is the budget BEFORE this line's own speaking time —
    # the browser ticks the displayed timer down from this value as the
    # audio plays, ending at (this value - audio.duration).
    await websocket.send_json(
        {"type": "customer_line", "text": text, "remaining_s": session.remaining_s()}
    )
    audio_bytes = await asyncio.to_thread(synthesize_wav, text)
    session.add_speaking_time(wav_duration_s(audio_bytes))
    session.check_timeout()
    await websocket.send_bytes(audio_bytes)


@app.websocket("/ws/call/{scenario_id}")
async def call_socket(websocket: WebSocket, scenario_id: str) -> None:
    await websocket.accept()

    scenario = SCENARIOS.get(scenario_id)
    if scenario is None:
        await websocket.send_json({"type": "error", "message": f"Unknown scenario: {scenario_id}"})
        await websocket.close()
        return

    try:
        session = CallSession(scenario)
    except Exception as exc:
        await websocket.send_json({"type": "error", "message": f"Could not start session: {exc}"})
        await websocket.close()
        return

    disconnected = False
    try:
        opening_text = await asyncio.to_thread(session.start)
        await _send_customer_line(websocket, session, opening_text)

        while not session.ended:
            message = await websocket.receive()

            if message.get("type") == "websocket.disconnect":
                disconnected = True
                break

            raw_text = message.get("text")
            if raw_text is not None:
                control = json.loads(raw_text)
                if control.get("type") == "end_call":
                    session.end_manually()
                    break
                if control.get("type") == "advance_time":
                    reply_text = await asyncio.to_thread(session.advance_time)
                    if reply_text is not None:
                        await _send_customer_line(websocket, session, reply_text)
                continue

            audio_bytes = message.get("bytes")
            if not audio_bytes:
                continue

            trainee_text, trainee_duration, trainee_confidence = await asyncio.to_thread(
                transcribe_webm, audio_bytes
            )
            await websocket.send_json(
                {"type": "trainee_turn_transcribed", "text": trainee_text or "(inaudible)"}
            )
            if not trainee_text:
                continue

            reply_text = await asyncio.to_thread(
                session.handle_trainee_turn, trainee_text, trainee_duration, trainee_confidence
            )
            if reply_text is not None:
                await _send_customer_line(websocket, session, reply_text)
            elif session.ended:
                break

        if not disconnected:
            await websocket.send_json({"type": "call_ended", "reason": session.end_reason})
            await websocket.send_json({"type": "evaluating"})

            findings = await asyncio.to_thread(evaluate_call, session.scenario, session.transcript)
            result = score_call(session.scenario, findings, session.end_reason)
            await websocket.send_json({"type": "evaluation_result", "result": result})

    except WebSocketDisconnect:
        disconnected = True
    except Exception as exc:
        try:
            await websocket.send_json({"type": "error", "message": f"Unexpected error: {exc}"})
        except Exception:
            pass
    finally:
        if not disconnected:
            await websocket.close()
