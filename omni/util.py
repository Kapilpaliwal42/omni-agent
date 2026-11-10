"""Shared helpers: secret redaction, clipping, command guards, TLS, HTML-to-text, tool-call parsing."""
from __future__ import annotations

import datetime as dt
import html
import json
import os
import platform
import re
import ssl
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # the omni-agent checkout (its own code)
# runtime state: logs, memory, MCP config, browser profile, staging. OMNI_STATE moves it (tests use a temp dir).
STATE = Path(os.environ.get("OMNI_STATE") or ROOT / ".omni")
MAX_TOOL_OUTPUT = 8_000

SECRET_PATTERNS = [
    re.compile(r"sk-(ant-|or-v1-)?[A-Za-z0-9_\-]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),
    re.compile(r"hf_[A-Za-z0-9]{30,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"[a-z][a-z0-9+.\-]*://[^\s:/@'\"]+:[^\s@/'\"]+(?=@)", re.I),
]

# Blocked in every mode: commands that can wreck the machine.
HARD_BLOCK = [
    r"\brm\s+-[a-z]*r[a-z]*f?\s+(/|~|\$HOME)\s*$",
    r"\bmkfs(\.\w+)?\b",
    r"\bformat\s+[a-z]:",
    r"\bdd\s+.*\bof=/dev/(sd|nvme|disk)",
    r":\(\)\s*\{\s*:\|:&\s*\};:",
    r"\b(shutdown|reboot|Stop-Computer|Restart-Computer)\b",
    r"\bdiskpart\b",
    r"\bbcdedit\b",
    r"Remove-Item\s+.*\b[A-Z]:\\\s*(-|$)",
]

# Confirmed in "auto" mode; run freely in "yolo".
RISKY = [
    r"\bgit\s+push\b", r"\bgit\s+(init|commit|rebase|merge|checkout\s+--)\b", r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+clean\b", r"\bgit\s+branch\s+-D\b",
    r"\brm\s+-[a-z]*r", r"Remove-Item\b.*-Recurse", r"\b(del|rd|rmdir)\s+/s\b",
    r"\b(curl|wget)\b.*\|\s*(sh|bash)", r"\b(iwr|irm|Invoke-WebRequest|Invoke-RestMethod)\b.*\|\s*iex",
    r"\b(npm|pnpm|yarn|cargo|twine)\s+publish\b", r"\bdocker\s+(rm|rmi|system\s+prune)\b",
    r"\bkubectl\s+(delete|apply)\b", r"\bterraform\s+(apply|destroy)\b",
    r"\b(DROP|TRUNCATE)\s+(TABLE|DATABASE)\b", r"\bgh\s+(pr|issue|release)\s+(create|merge|close)\b",
    r"\b(pip|npm|winget|choco|scoop)\s+(install|uninstall)\b", r"\breg\s+(add|delete)\b", r"Set-ItemProperty\b.*HK",
    r"\bschtasks\b", r"\bsc\s+(create|delete|config)\b", r"Set-ExecutionPolicy",
]


def redact(text: str) -> str:
    for pat in SECRET_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    return text


def clip(text: str, limit: int = MAX_TOOL_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n\n... [{len(text) - limit} chars truncated] ...\n\n{text[-half:]}"


def matches(patterns: list[str], s: str) -> bool:
    return any(re.search(p, s, re.IGNORECASE) for p in patterns)


def html_to_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h[1-6]|tr|pre)>", "\n", raw)
    raw = html.unescape(re.sub(r"(?s)<[^>]+>", " ", raw))
    raw = re.sub(r"[ \t\r\f\v]+", " ", raw)
    return re.sub(r"\n\s*\n+", "\n\n", raw).strip()


def extract_calls(text: str, names: set[str]) -> list[tuple[str, dict]]:
    """Pull {"name": ..., "arguments": {...}} tool calls out of model text (fenced, tagged, or bare)."""
    s = re.sub(r"</?tool_call>|```(?:json)?", " ", text or "")
    dec, calls, i = json.JSONDecoder(), [], 0
    while (j := s.find("{", i)) >= 0:
        try:
            obj, end = dec.raw_decode(s, j)
        except ValueError:
            i = j + 1
            continue
        i = end
        if isinstance(obj, dict) and obj.get("name") in names:
            args = obj.get("arguments", obj.get("parameters", {}))
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    continue
            if isinstance(args, dict):
                calls.append((obj["name"], args))
    return calls


def command_signature(cmd: str) -> str:
    cmd = re.sub(r"^\s*(cd|Set-Location)\s+(\"[^\"]*\"|'[^']*'|\S+)\s*(&&|;)\s*", "", cmd or "", flags=re.I)
    return re.sub(r"\s+", " ", cmd).strip()[:120]


def syntax_problem(f: Path) -> str | None:
    try:
        if f.suffix == ".py":
            compile(f.read_text(encoding="utf-8"), str(f), "exec")
        elif f.suffix == ".json":
            json.loads(f.read_text(encoding="utf-8"))
    except SyntaxError as e:
        return f"SyntaxError in {f.name} line {e.lineno}: {e.msg}\n  {(e.text or '').rstrip()}"
    except ValueError as e:
        return f"Invalid JSON in {f.name}: {e}"
    return None


_SSL_CTX = None


def ssl_context() -> ssl.SSLContext:
    """Verified TLS. Falls back to known CA bundles and, on Windows Pythons that can't read the system
    store, to an export of the Windows roots (TLS-scanning antivirus installs its root there)."""
    global _SSL_CTX
    if _SSL_CTX is None:
        ctx = ssl.create_default_context()
        if ctx.cert_store_stats().get("x509_ca", 0) == 0:
            root = Path(sys.prefix).parent
            for cand in (os.environ.get("SSL_CERT_FILE"), root / "usr/ssl/certs/ca-bundle.crt",
                         Path(sys.prefix) / "etc/ssl/cert.pem"):
                if cand and Path(cand).is_file():
                    ctx.load_verify_locations(cafile=str(cand))
                    break
        if platform.system() == "Windows" and not hasattr(ssl, "enum_certificates"):
            pem = _windows_roots_pem()
            if pem:
                ctx.load_verify_locations(cafile=str(pem))
        _SSL_CTX = ctx
    return _SSL_CTX


def _windows_roots_pem() -> Path | None:
    pem = STATE / "cache" / "windows-roots.pem"
    if not (pem.is_file() and dt.datetime.now().timestamp() - pem.stat().st_mtime < 7 * 86400):
        script = ("$c = Get-ChildItem Cert:\\LocalMachine\\Root, Cert:\\CurrentUser\\Root | Sort-Object Thumbprint -Unique; "
                  "$c | ForEach-Object { '-----BEGIN CERTIFICATE-----'; "
                  "[Convert]::ToBase64String($_.RawData, 'InsertLineBreaks'); '-----END CERTIFICATE-----' }")
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                               capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if r.returncode != 0 or "BEGIN CERTIFICATE" not in r.stdout:
            return None
        pem.parent.mkdir(parents=True, exist_ok=True)
        pem.write_text(r.stdout, encoding="ascii", errors="ignore")
    return pem
