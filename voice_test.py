"""
voice_test.py

Full voice loop: press Enter, speak, press Enter again to stop — your
speech is transcribed (Whisper), sent to the AI customer (GPT), and the
customer's reply is spoken back to you (TTS-HD). Same scenario/agent
logic as text_test.py, just with a microphone/speaker loop instead of a
keyboard prompt.

Design notes (avoiding the pain points from earlier sounddevice apps):
  - Push-to-talk via Enter key, not voice activity detection — no risk
    of clipping the start of speech or mis-detecting silence.
  - Everything happens in memory (numpy arrays / BytesIO) — no temp
    WAV files on disk, so no file-lock/permission issues on Windows.
  - TTS is requested directly as WAV (not mp3), so playback doesn't need
    an mp3 decoder — just the stdlib `wave` module + sounddevice.

Usage:
    python voice_test.py
    python voice_test.py --scenario scenarios/data_protection/dp_001.json
"""

import argparse
import io
import json
import os
import queue
import threading
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd
from dotenv import load_dotenv
from openai import AzureOpenAI

load_dotenv()

SAMPLE_RATE = 16000
CHANNELS = 1
MAX_RECORDING_SECONDS = 90

# ==========================================
# AZURE CLIENTS
# ==========================================


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Copy .env.example to .env and fill in your real Azure AI Foundry values."
        )
    return value


GPT_MODEL = os.environ.get("GPT_MODEL", "gpt-5-mini")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "whisper")
TTS_MODEL = os.environ.get("TTS_MODEL", "tts-hd")
TTS_VOICE = os.environ.get("TTS_VOICE", "alloy")

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


class ScenarioValidationError(Exception):
    pass


def load_scenario(path: str) -> dict:
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"Scenario file not found: {path}")

    with open(file_path, "r", encoding="utf-8") as f:
        scenario = json.load(f)

    missing = [k for k in REQUIRED_TOP_LEVEL_KEYS if k not in scenario]
    if missing:
        raise ScenarioValidationError(f"Scenario '{path}' is missing required keys: {missing}")

    return scenario


def get_customer_safe_view(scenario: dict) -> dict:
    return {
        "customer": scenario["customer"],
        "case": scenario["case"],
    }


# ==========================================
# CUSTOMER PROMPT
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

# OUTPUT FORMAT — MANDATORY
You must respond with ONLY a valid JSON object, no markdown fences, no extra text, in exactly this shape:

{{"customer_message": "<what you say out loud, in character>", "call_should_end": <true or false>, "end_reason": "<short reason, or null if false>"}}

Set "call_should_end": true only when the conversation has reached a natural close (you've gotten a clear next step and have nothing more to ask, or you need to go, or you've been on the call an unreasonably long time). Otherwise always false.

# SCENARIO FOR THIS CALL
{SCENARIO_BLOCK}
"""

# ==========================================
# CUSTOMER AGENT
# ==========================================


class CustomerAgentError(Exception):
    pass


class CustomerAgent:
    def __init__(self, scenario: dict):
        self.scenario = scenario
        safe_view = get_customer_safe_view(scenario)

        scenario_block = json.dumps(safe_view, indent=2, ensure_ascii=False)
        system_prompt = CUSTOMER_PROMPT_TEMPLATE.replace("{SCENARIO_BLOCK}", scenario_block)

        self.history = [{"role": "system", "content": system_prompt}]
        self._opening_sent = False

    def get_opening_message(self) -> dict:
        opening_line = self.scenario["customer"]["opening_line"]
        turn = {
            "customer_message": opening_line,
            "call_should_end": False,
            "end_reason": None,
        }
        self.history.append({"role": "assistant", "content": json.dumps(turn)})
        self._opening_sent = True
        return turn

    def respond(self, trainee_message: str) -> dict:
        if not self._opening_sent:
            raise CustomerAgentError("Call get_opening_message() once before respond().")

        self.history.append({"role": "user", "content": trainee_message})

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


# ==========================================
# AUDIO: RECORD (push-to-talk, in memory)
# ==========================================


def record_until_enter() -> np.ndarray:
    """Records from the mic starting immediately; stops when Enter is
    pressed (or after MAX_RECORDING_SECONDS as a safety net). Returns a
    1-D int16 numpy array. No temp files, no VAD."""
    q: queue.Queue = queue.Queue()
    stop_event = threading.Event()

    def callback(indata, frames, time_info, status):
        q.put(indata.copy())

    def wait_for_enter():
        input()
        stop_event.set()

    listener = threading.Thread(target=wait_for_enter, daemon=True)
    listener.start()

    chunks = []
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="int16", callback=callback):
        print("Recording... (press Enter to stop)")
        elapsed = 0.0
        while not stop_event.is_set() and elapsed < MAX_RECORDING_SECONDS:
            try:
                chunk = q.get(timeout=0.1)
                chunks.append(chunk)
                elapsed += len(chunk) / SAMPLE_RATE
            except queue.Empty:
                continue

    if not chunks:
        return np.zeros((0, CHANNELS), dtype="int16")
    return np.concatenate(chunks, axis=0)


def audio_array_to_wav_bytes(audio: np.ndarray, samplerate: int = SAMPLE_RATE) -> io.BytesIO:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)  # int16
        wf.setframerate(samplerate)
        wf.writeframes(audio.tobytes())
    buf.seek(0)
    buf.name = "trainee_turn.wav"
    return buf


def transcribe(audio: np.ndarray) -> str:
    duration_s = len(audio) / SAMPLE_RATE
    if duration_s < 0.3:
        return ""
    wav_buf = audio_array_to_wav_bytes(audio)
    response = whisper_client.audio.transcriptions.create(
        model=WHISPER_MODEL,
        file=wav_buf,
    )
    return (response.text or "").strip()


# ==========================================
# AUDIO: SPEAK (TTS-HD, in memory)
# ==========================================


def speak(text: str, voice: str = TTS_VOICE) -> None:
    response = tts_client.audio.speech.create(
        model=TTS_MODEL,
        voice=voice,
        input=text,
        response_format="wav",
    )
    audio_bytes = response.read()

    buf = io.BytesIO(audio_bytes)
    with wave.open(buf, "rb") as wf:
        samplerate = wf.getframerate()
        n_channels = wf.getnchannels()
        raw = wf.readframes(wf.getnframes())

    audio_array = np.frombuffer(raw, dtype=np.int16)
    if n_channels > 1:
        audio_array = audio_array.reshape(-1, n_channels)

    sd.play(audio_array, samplerate)
    sd.wait()


# ==========================================
# CALL LOOP
# ==========================================

DEFAULT_SCENARIO = "scenarios/data_protection/dp_001.json"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", default=DEFAULT_SCENARIO)
    args = parser.parse_args()

    scenario = load_scenario(args.scenario)
    agent = CustomerAgent(scenario)

    print(f"--- Scenario: {scenario['scenario_id']} — {scenario['area']} ---")
    print("(Press Enter to start speaking, Enter again to stop. Ctrl+C to end the call.)\n")

    opening = agent.get_opening_message()
    print(f"CUSTOMER: {opening['customer_message']}")
    speak(opening["customer_message"])

    try:
        while True:
            print("\nYour turn — press Enter to start talking.")
            input()
            audio = record_until_enter()
            trainee_text = transcribe(audio)

            if not trainee_text:
                print("(Didn't catch that — nothing usable was recorded. Try again.)")
                continue

            print(f"YOU: {trainee_text}")

            try:
                turn = agent.respond(trainee_text)
            except CustomerAgentError as e:
                print(f"[ERROR] {e}")
                continue

            print(f"CUSTOMER: {turn['customer_message']}")
            speak(turn["customer_message"])

            if turn["call_should_end"]:
                reason = turn["end_reason"] or "not specified"
                print(f"\n--- Call ended by customer agent. Reason: {reason} ---")
                break
    except KeyboardInterrupt:
        print("\n--- Call ended manually ---")


if __name__ == "__main__":
    main()
