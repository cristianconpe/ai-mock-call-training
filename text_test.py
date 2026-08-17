"""
text_test.py

Single-file text-only test harness for the AI customer agent — no audio
involved. Everything (Azure clients, scenario loading, the customer agent,
and its prompt) lives in this one file on purpose, to avoid cross-file
import/config drift while we're still nailing down the right Azure
endpoint/api-version combination.

Confirmed working setup against the hack2026win-4020-resource Foundry
resource (all three deployments live on this ONE resource):
  - GPT_ENDPOINT / WHISPER_ENDPOINT / TTS_ENDPOINT: the bare resource
    endpoint, e.g. https://hack2026win-4020-resource.services.ai.azure.com
    (no /api/projects/... suffix, no /openai/... suffix — the SDK builds
    that path itself from the deployment name).
  - Chat completions api-version: 2024-10-21
  - Whisper transcription api-version: 2024-06-01
  - TTS speech api-version: 2024-12-01-preview

Usage:
    python text_test.py
    python text_test.py --scenario scenarios/data_protection/dp_001.json
"""

import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from openai import AzureOpenAI

load_dotenv()

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
    """Only the data the customer agent is allowed to see — persona/behavior
    and case framing. Deliberately excludes knowledge_base and evaluation,
    so the KB can never leak into the customer's own responses."""
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
        """Returns the fixed opening line from the scenario as the first
        customer turn, without calling the model — keeps every trainee's
        first experience identical for a given scenario."""
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
        """Sends the trainee's message and returns the parsed JSON turn:
        {"customer_message": str, "call_should_end": bool, "end_reason": str|None}"""
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
# INTERACTIVE TEST LOOP
# ==========================================

DEFAULT_SCENARIO = "scenarios/data_protection/dp_001.json"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", default=DEFAULT_SCENARIO)
    args = parser.parse_args()

    scenario = load_scenario(args.scenario)
    agent = CustomerAgent(scenario)

    print(f"--- Scenario: {scenario['scenario_id']} — {scenario['area']} ---")
    print("(Type your reply as the support engineer. Type 'exit' to quit.)\n")

    opening = agent.get_opening_message()
    print(f"CUSTOMER: {opening['customer_message']}\n")

    while True:
        trainee_input = input("YOU: ").strip()
        if trainee_input.lower() in ("exit", "quit"):
            print("\n--- Call ended manually ---")
            break
        if not trainee_input:
            continue

        try:
            turn = agent.respond(trainee_input)
        except CustomerAgentError as e:
            print(f"\n[ERROR] {e}\n")
            continue

        print(f"CUSTOMER: {turn['customer_message']}\n")

        if turn["call_should_end"]:
            reason = turn["end_reason"] or "not specified"
            print(f"--- Call ended by customer agent. Reason: {reason} ---")
            break


if __name__ == "__main__":
    main()
