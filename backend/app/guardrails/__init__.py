"""
Guardrails: input sanitisation, PII redaction and prompt-injection detection.

The regexes and the canned user-facing strings live in this package's YAML
files rather than in Python literals, so the rule set is editable without a
code change and unit-testable in isolation.
"""
import functools
import re
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Pattern, Tuple

import yaml

GUARDRAILS_DIR = Path(__file__).resolve().parent


class GuardrailError(ValueError):
    """Raised when a guardrail YAML file is missing or malformed."""


class CompiledPattern(NamedTuple):
    regex: Pattern[str]
    replacement: Optional[str]
    flags: int


_FLAG_NAMES = {"I": re.IGNORECASE, "M": re.MULTILINE, "S": re.DOTALL}


def _parse_flags(raw: Optional[str]) -> int:
    flags = 0
    for char in raw or "":
        if char.upper() not in _FLAG_NAMES:
            raise GuardrailError(f"Unknown regex flag '{char}' in guardrail config")
        flags |= _FLAG_NAMES[char.upper()]
    return flags


@functools.lru_cache(maxsize=1)
def _patterns_doc() -> Dict:
    path = GUARDRAILS_DIR / "patterns.yaml"
    if not path.exists():
        raise GuardrailError(f"Guardrail patterns not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    if not isinstance(doc, dict):
        raise GuardrailError("patterns.yaml must contain a mapping at the top level")
    return doc


@functools.lru_cache(maxsize=1)
def _templates_doc() -> Dict[str, str]:
    path = GUARDRAILS_DIR / "templates.yaml"
    if not path.exists():
        raise GuardrailError(f"Guardrail templates not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    if not isinstance(doc, dict):
        raise GuardrailError("templates.yaml must contain a mapping at the top level")
    return {str(k): ("" if v is None else str(v)) for k, v in doc.items()}


# --- Patterns ---

@functools.lru_cache(maxsize=1)
def pii_patterns() -> Tuple[CompiledPattern, ...]:
    compiled = []
    for entry in _patterns_doc().get("pii_patterns") or []:
        compiled.append(
            CompiledPattern(
                regex=re.compile(entry["pattern"], _parse_flags(entry.get("flags"))),
                replacement=entry["replacement"],
                flags=_parse_flags(entry.get("flags")),
            )
        )
    return tuple(compiled)


@functools.lru_cache(maxsize=1)
def injection_patterns() -> Tuple[CompiledPattern, ...]:
    compiled = []
    for entry in _patterns_doc().get("injection_patterns") or []:
        compiled.append(
            CompiledPattern(
                regex=re.compile(entry["pattern"], _parse_flags(entry.get("flags"))),
                replacement=None,
                flags=_parse_flags(entry.get("flags")),
            )
        )
    return tuple(compiled)


def allowed_control_chars() -> List[str]:
    return list(_patterns_doc().get("allowed_control_chars") or [])


def max_message_chars() -> int:
    return int(_patterns_doc().get("max_message_chars", 500))


# --- Templates ---

def template(name: str, **kwargs) -> str:
    """Fetch a canned string by key and format it with the supplied fields."""
    doc = _templates_doc()
    if name not in doc:
        raise GuardrailError(f"Unknown guardrail template '{name}'")
    text = doc[name]
    if not kwargs:
        return text
    return text.format(**kwargs)


# --- Sanitisers ---

def strip_control_chars(text: str) -> str:
    keep = set(allowed_control_chars())
    return "".join(ch for ch in text if ch >= " " or ch in keep)


def redact_pii(text: str) -> str:
    scrubbed = text
    for pattern in pii_patterns():
        scrubbed = pattern.regex.sub(pattern.replacement or "", scrubbed)
    return scrubbed


def detect_injection(text: str) -> bool:
    return any(pattern.regex.search(text) for pattern in injection_patterns())


def sanitize_message(message: str) -> Tuple[str, bool]:
    """
    Return `(sanitized_message, injection_flag)`.

    Strips control characters, redacts PII, then scans for injection attempts.
    The length cap is applied first so a hostile payload cannot force
    expensive regex work.
    """
    clipped = (message or "")[: max_message_chars()]
    sanitized = redact_pii(strip_control_chars(clipped).strip())
    return sanitized, detect_injection(sanitized)