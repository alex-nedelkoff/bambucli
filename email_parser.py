"""Patron-email parser. Regex first-pass + local Ollama for prose-y fields.

Pipeline:
  1. Regex pulls out a 14-digit Ajax library card if it's anywhere in the body.
  2. Ollama (running on the host at localhost:11434) does the natural-language
     parse — assigns a colour and a quantity to each attached STL filename,
     extracts the patron's name.
  3. Final dict is form-ready: customer, card, colors (CSV), quantity (CSV),
     in the same per-STL order as `stl_names`.

If Ollama is unreachable, we fall back to whatever the regex found and leave
the rest blank — the staff review screen catches missing fields visually.

Stdlib only. No dependencies.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request


OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "llama3.2:3b"
OLLAMA_TIMEOUT_S = 30

# Ajax library cards are 14 digits; allow patron-typed dashes/dots/spaces
# between the four-character groups.
_CARD_RE = re.compile(r"\b(\d{4}[\s\-.]?\d{4}[\s\-.]?\d{4}[\s\-.]?\d{2})\b")


def regex_card(body: str) -> str | None:
    """Extract a 14-digit library card from anywhere in the body. Returns
    digits-only string, or None if no plausible card is found."""
    m = _CARD_RE.search(body)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(1))
    return digits if len(digits) == 14 else None


def _ollama_call(body: str, stl_names: list[str], from_header: str) -> dict:
    """Single-shot call to Ollama with format=json. Returns parsed dict on
    success, empty dict on any failure (network, parse, model misbehaviour)."""
    stl_list = "\n".join(f"  - {n}" for n in stl_names) or "  (none)"
    prompt = (
        "You are parsing a 3D-print order email from a public-library patron.\n"
        f"Email From: {from_header}\n"
        "Attached STL filenames:\n"
        f"{stl_list}\n\n"
        "Email body:\n\"\"\"\n"
        f"{body.strip()}\n"
        "\"\"\"\n\n"
        "Return ONLY valid JSON in exactly this shape:\n"
        "{\n"
        '  "customer": "<patron full name>",\n'
        '  "card": "<14-digit library card, digits only>",\n'
        '  "stl_assignments": [\n'
        '    {"filename": "<exact STL filename from list above>",\n'
        '     "color": "<one of: Red, Blue, Black, White, Grey, Green, Yellow, '
        'Orange, Purple, Pink, Brown, Beige, Clear, Gold, Silver>",\n'
        '     "quantity": <integer>}\n'
        "  ]\n"
        "}\n\n"
        "Rules:\n"
        "- Use the EXACT STL filenames from the attached list. Do not invent.\n"
        "- Map fuzzy colour words generously: 'dark red' -> Red, 'navy' -> Blue,\n"
        "  'forest green' -> Green, 'charcoal' -> Black.\n"
        "- If one colour is mentioned for the whole order, apply it to every STL.\n"
        "- If a quantity isn't specified per file, default to 1.\n"
        "- If a field can't be determined, leave it as an empty string or 0.\n"
    )
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "format": "json",
    }
    try:
        req = urllib.request.Request(
            OLLAMA_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT_S) as r:
            body_resp = json.loads(r.read().decode("utf-8"))
        return json.loads(body_resp.get("response", "{}") or "{}")
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError, OSError):
        return {}


def parse_email(body: str, stl_names: list[str], from_header: str = "") -> dict:
    """Parse a patron email into form-ready fields. Always returns a complete
    dict; fields the parser couldn't determine come back empty.

    Returns:
        {
          "customer":    str,
          "card":        str,        # digits-only, may be empty
          "colors":      str,        # CSV, one per STL in stl_names order
          "quantity":    str,        # CSV, one per STL in stl_names order
          "ollama_used": bool,       # diagnostic — was the LLM reachable?
        }
    """
    regex_pulled = regex_card(body)
    llm = _ollama_call(body, stl_names, from_header)

    # Customer: trust the LLM
    customer = str(llm.get("customer", "")).strip()

    # Card: prefer the LLM if it gave us 14 valid digits, else regex
    llm_card_digits = re.sub(r"\D", "", str(llm.get("card", "")))
    if len(llm_card_digits) == 14:
        card = llm_card_digits
    else:
        card = regex_pulled or ""

    # Build per-STL color/qty in the same order as stl_names
    by_filename = {a.get("filename"): a for a in (llm.get("stl_assignments") or [])
                   if isinstance(a, dict)}
    colors: list[str] = []
    qtys: list[str] = []
    for stl in stl_names:
        a = by_filename.get(stl, {})
        colors.append(str(a.get("color", "")).strip())
        try:
            q = int(a.get("quantity", 1))
            qtys.append(str(q if q > 0 else 1))
        except (TypeError, ValueError):
            qtys.append("1")

    return {
        "customer":    customer,
        "card":        card,
        "colors":      ",".join(colors),
        "quantity":    ",".join(qtys),
        "ollama_used": bool(llm),
    }
