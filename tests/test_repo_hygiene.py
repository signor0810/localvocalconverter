"""Guard against publishing private endpoints or credentials.

Scans every git-tracked text file for things that must never reach a public repository:
private/VPN IP addresses, strings shaped like API keys, and private key blocks.
Loopback and documentation placeholders are allowed."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
THIS = Path(__file__).resolve()

_IP = re.compile(r"(?<![\d.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\d.])")
_SECRETS = [
    ("api key", re.compile(r"\b(?:sk|pk)-[A-Za-z0-9_\-]{20,}")),
    ("github token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("hugging face token", re.compile(r"\bhf_[A-Za-z0-9]{30,}")),
    ("aws key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("bearer token", re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]{32,}")),
    ("tailnet host", re.compile(r"\b[\w\-]+\.ts\.net\b")),
]


def _is_private_ip(a: int, b: int, c: int, d: int) -> bool:
    if max(a, b, c, d) > 255:
        return False
    return (
        a == 10
        or (a == 172 and 16 <= b <= 31)
        or (a == 192 and b == 168)
        or (a == 100 and 64 <= b <= 127)   # CGNAT range used by mesh VPNs
        or (a == 169 and b == 254)
    )


def _tracked_files() -> list:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True, timeout=30
        ).stdout
    except Exception:
        pytest.skip("not a git checkout")
    return [ROOT / n for n in out.decode("utf-8", "replace").split("\0") if n]


def find_problems(text: str) -> list:
    found = []
    for m in _IP.finditer(text):
        if _is_private_ip(*(int(g) for g in m.groups())):
            found.append(f"private IP {m.group(0)}")
    for label, pattern in _SECRETS:
        if pattern.search(text):
            found.append(label)
    return found


def test_detector_catches_known_shapes():
    assert find_problems("http://100." + "77.1.2:8000/v1")
    assert find_problems("host 192." + "168.1.10")
    assert find_problems("key sk-" + "a" * 24)
    assert find_problems("-----BEGIN RSA " + "PRIVATE KEY-----")
    assert not find_problems("http://localhost:8000/v1 and 127.0.0.1 and version 1.2.3.4")


def test_no_private_endpoints_or_keys_in_tracked_files():
    problems = []
    for path in _tracked_files():
        if path.resolve() == THIS or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable
        for issue in find_problems(text):
            problems.append(f"{path.relative_to(ROOT)}: {issue}")
    assert not problems, "private data in tracked files:\n" + "\n".join(problems)
