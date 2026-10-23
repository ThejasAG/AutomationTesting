"""Nothing committed may contain a real credential. Git history is forever.

The seed is COMMITTED and originally carried live staging passwords in its scenario
steps ("type 2A_emp@… in passwordValue"). It now carries ${VAR} placeholders and the
importing machine supplies its own from .env.
"""
import json
import re
from pathlib import Path

SEED = Path("seeds/platform.json")
EXAMPLE = Path(".env.example")

# Provider key shapes: Groq, GitHub, OpenAI, Slack.
_KEY = re.compile(r"\b(gsk_[A-Za-z0-9]{20,}|gh[pousr]_[A-Za-z0-9]{20,}"
                  r"|sk-[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,})")


def _steps(s):
    return json.loads(s["steps"]) if isinstance(s["steps"], str) else (s["steps"] or [])


def test_the_seed_has_no_provider_keys():
    assert not _KEY.search(SEED.read_text())


def test_the_seed_has_no_plaintext_app_credentials():
    """A password reaches the seed embedded in a sentence, not as its own field."""
    for s in json.loads(SEED.read_text())["scenarios"]:
        for st in _steps(s):
            low = st.lower()
            if "password" in low and "${" not in st:
                assert False, f"plaintext credential in {s['name']}: {st}"


def test_credential_steps_use_placeholders():
    """And they must be placeholders the importing machine can actually resolve."""
    seen = set()
    for s in json.loads(SEED.read_text())["scenarios"]:
        for st in _steps(s):
            seen.update(re.findall(r"\$\{([A-Z_]+)\}", st))
    assert seen, "no credential placeholders — did redaction silently stop working?"
    example = EXAMPLE.read_text()
    for var in seen:
        assert f"{var}=" in example, f"{var} is used but not documented in .env.example"


def test_the_example_env_has_no_real_keys():
    assert not _KEY.search(EXAMPLE.read_text())


def test_the_example_documents_the_settings_that_fail_silently():
    text = EXAMPLE.read_text()
    assert "DATABASE_URL" in text and "OUTSIDE this repo" in text
    assert "LLM_PROVIDER_TYPE" in text and "DOES NOT ERROR" in text


def test_dotenv_itself_is_not_tracked():
    import subprocess
    tracked = subprocess.run(["git", "ls-files", ".env"], capture_output=True, text=True).stdout
    assert not tracked.strip(), ".env is tracked by git — it holds live keys"
