"""Self-update benchmark: realistic "change yourself" requests, each scored by a hidden test.

For every task the benchmark:
  1. clones the agent's code into a temp folder (the committed HEAD, or --working-tree for uncommitted code),
     optionally injecting a bug the request asks to fix;
  2. runs that clone's own self_improve(request) on the real model. Only the final "Apply this change?"
     prompt is approved; every other permission prompt is denied, so nothing outside the clone is touched;
  3. scores it: was a change applied, does the clone's own test suite still pass, and does a HIDDEN test
     (never shown to the agent) confirm the requested behaviour really works.

    .venv\\Scripts\\python bench\\selfupdate_bench.py --label baseline
    .venv\\Scripts\\python bench\\selfupdate_bench.py --label improved --working-tree --only word_count,clip_bug

Results: bench/results/selfupdate-<label>.jsonl plus a summary table printed at the end.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable

AGENT_FACTORY = """
import os, sys
sys.path.insert(0, CLONE)
from omni.core import Agent, Config
def make_agent():
    ws = os.path.join(os.environ["OMNI_STATE"], "hidden-ws")
    os.makedirs(ws, exist_ok=True)
    return Agent(Config(workspace=ws, rules="", mode="yolo", web=True),
                 chat_fn=lambda p: {"message": {"content": "ok"}, "done_reason": "stop"}), ws
"""

TASKS = [
    {
        "name": "word_count",
        "request": "Add a new built-in tool named word_count with one argument, path. It returns the number of "
                   "words in that text file in the workspace, as text like 'words: 4'. Register it in the core "
                   "toolset next to read_file.",
        "hidden": """
a, ws = make_agent()
open(os.path.join(ws, "w.txt"), "w").write("one two  three\\nfour")
out, err = a.execute("word_count", {"path": "w.txt"})
assert not err, out
assert "4" in out, out
""",
    },
    {
        "name": "version_cmd",
        "request": "Add a /version slash command that prints the agent's current git commit (short hash) and "
                   "the model name. It must show up in /help like the other commands.",
        "hidden": """
import subprocess
from omni.commands import is_command, run_command
a, ws = make_agent()
sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=CLONE, capture_output=True, text=True).stdout.strip()
assert is_command("/version"), "not a command"
out = run_command(a, "/version")
assert sha[:6] in out, (sha, out)
assert a.cfg.model in out, out
assert "/version" in run_command(a, "/help")
""",
    },
    {
        "name": "fetch_localhost",
        "request": "For safety, make the web_fetch tool refuse local and private addresses (localhost, 127.x, "
                   "10.x, 192.168.x, 172.16-31.x and ::1) without connecting, returning an error that says "
                   "'local addresses are not allowed'. Public URLs must keep working as before.",
        "hidden": """
a, ws = make_agent()
for u in ["http://localhost:8765/x", "http://127.0.0.1/", "https://192.168.1.10/a", "http://10.0.0.5", "http://[::1]/"]:
    out, err = a.execute("web_fetch", {"url": u})
    assert err, (u, out)
    assert "not allowed" in out.lower(), (u, out)
    assert "Network error" not in out, (u, out)
import omni.builtin as b
b.http_get = lambda url, *x, **k: "<html><body><p>hello public</p></body></html>"
a2, _ = make_agent()
out, err = a2.execute("web_fetch", {"url": "https://example.com/"})
assert not err and "hello public" in out, out
""",
    },
    {
        "name": "clip_bug",
        "inject": ("omni/util.py", "    half = limit // 2\n    return f\"{text[:half]}",
                   "    half = limit // 8\n    return f\"{text[:half]}"),
        "request": "Bug: clip() in omni/util.py keeps far too little of long text. For text longer than the limit "
                   "it should keep about `limit` characters: the first half of the limit from the start and the "
                   "other half from the end, with a truncation note in between. Fix it and add a test.",
        "hidden": """
from omni.util import clip
s = "A" * 5000 + "Z" * 5000
out = clip(s, 1000)
assert 1000 <= len(out) <= 1200, len(out)
assert out.startswith("A" * 500) and out.endswith("Z" * 500), out[:20]
assert clip("short", 1000) == "short"
""",
    },
]

RUNNER = r"""
import json, os, sys, time
CLONE = sys.argv[1]
sys.path.insert(0, CLONE)
from omni.core import Agent, Config
cfg = Config(workspace=CLONE, rules=os.path.join(CLONE, "rules.md"), mode="auto", model=sys.argv[3], web=False)
extra = json.loads(sys.argv[4])
cfg.extra.update(extra.get("extra", {}))
for k, v in extra.get("cfg", {}).items():
    setattr(cfg, k, v)
a = Agent(cfg, ui_input=lambda p: "n")
def confirm(summary, *rest):
    ok = summary.startswith("Apply this change")
    print("[bench] confirm ->", "yes" if ok else "no", "|", summary.splitlines()[0][:100], flush=True)
    return ok
a.confirm_fn = confirm
a.ask_fn = lambda q: ""
t = time.time()
out, err = a.self_improve(sys.argv[2])
print("BENCH_RESULT " + json.dumps({"out": out[-3000:], "err": err, "secs": round(time.time() - t)}), flush=True)
a.close()
"""


def sh(args, cwd, timeout=600, env=None):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, env=env)


def make_clone(dest: Path, working_tree: bool):
    sh(["git", "clone", "-q", str(ROOT), str(dest)], ROOT)
    for k, v in (("user.email", "bench@example.invalid"), ("user.name", "bench"), ("commit.gpgsign", "false")):
        sh(["git", "config", k, v], dest)
    if working_tree:   # overlay uncommitted code so it can be benchmarked before committing
        files = sh(["git", "ls-files", "--cached", "--others", "--exclude-standard"], ROOT).stdout.split()
        for f in files:
            src = ROOT / f
            if src.is_file() and not f.startswith(("bench/results", ".omni")):
                (dest / f).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest / f)
        for f in sh(["git", "ls-files"], dest).stdout.split():
            if not (ROOT / f).exists():
                (dest / f).unlink(missing_ok=True)
        sh(["git", "add", "-A"], dest)
        sh(["git", "commit", "-q", "-m", "bench: working tree"], dest)


def run_task(task: dict, args) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix=f"omni-bench-{task['name']}-"))
    clone, state = tmp / "repo", tmp / "state"
    state.mkdir()
    make_clone(clone, args.working_tree)
    if "inject" in task:
        path, old, new = task["inject"]
        f = clone / path
        text = f.read_text(encoding="utf-8")
        assert old in text, f"injection anchor missing in {path}"
        f.write_text(text.replace(old, new, 1), encoding="utf-8")
        sh(["git", "commit", "-qam", "bench: inject bug"], clone)
    head0 = sh(["git", "rev-parse", "HEAD"], clone).stdout.strip()
    env = os.environ | {"OMNI_STATE": str(state), "PYTHONIOENCODING": "utf-8"}
    env.pop("OMNI_TOKEN", None)
    runner = tmp / "runner.py"
    runner.write_text(RUNNER, encoding="utf-8")
    t = time.time()
    log, timed_out = "(dry run: agent not started)", False
    try:
        if not args.dry:
            r = sh([PY, str(runner), str(clone), task["request"], args.model, json.dumps(json.loads(args.extra))],
                   tmp, timeout=args.timeout, env=env)
            log = r.stdout + r.stderr
    except subprocess.TimeoutExpired as e:
        log, timed_out = (e.stdout or "") + (e.stderr or "") if isinstance(e.stdout, str) else "", True
    secs = round(time.time() - t)
    res = {}
    for line in log.splitlines():
        if line.startswith("BENCH_RESULT "):
            res = json.loads(line[len("BENCH_RESULT "):])
    applied = sh(["git", "rev-parse", "HEAD"], clone).stdout.strip() != head0
    suite = sh([PY, "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-q"], clone, timeout=900,
               env=env | {"OMNI_STATE": str(state / "suite")})
    hidden_src = f"CLONE = {str(clone)!r}\n" + AGENT_FACTORY + task["hidden"] + "\nprint('HIDDEN_OK')\n"
    hidden_file = tmp / "hidden.py"
    hidden_file.write_text(hidden_src, encoding="utf-8")
    hidden = sh([PY, str(hidden_file)], clone, timeout=300, env=env | {"OMNI_STATE": str(state / "hidden")})
    row = {"task": task["name"], "applied": applied, "suite_ok": suite.returncode == 0,
           "hidden_ok": "HIDDEN_OK" in hidden.stdout, "secs": secs, "timed_out": timed_out,
           "agent_said": (res.get("out") or "")[:600], "hidden_error": (hidden.stdout + hidden.stderr)[-800:],
           "suite_tail": (suite.stdout + suite.stderr)[-600:], "diff_stat": sh(["git", "diff", "--stat", head0, "HEAD"], clone).stdout[-800:]}
    out_dir = ROOT / "bench" / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"selfupdate-{args.label}-{task['name']}.log").write_text(log, encoding="utf-8")
    if not args.keep:
        shutil.rmtree(tmp, ignore_errors=True)
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="run")
    ap.add_argument("--model", default=os.environ.get("OMNI_MODEL", "huihui_ai/qwen3.5-abliterated:9b"))
    ap.add_argument("--only", default="")
    ap.add_argument("--working-tree", action="store_true", help="benchmark uncommitted code too")
    ap.add_argument("--timeout", type=int, default=2400, help="seconds per task")
    ap.add_argument("--extra", default="{}", help='JSON: {"cfg": {...}, "extra": {...}} applied to the Config')
    ap.add_argument("--keep", action="store_true", help="keep temp clones for inspection")
    ap.add_argument("--dry", action="store_true", help="don't run the agent: checks every hidden test fails as-is")
    args = ap.parse_args()
    tasks = [t for t in TASKS if not args.only or t["name"] in args.only.split(",")]
    out = ROOT / "bench" / "results" / f"selfupdate-{args.label}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for t in tasks:
        print(f"[{args.label}] {t['name']} ...", flush=True)
        row = run_task(t, args)
        rows.append(row)
        with out.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        print(f"[{args.label}] {t['name']}: applied={row['applied']} suite={row['suite_ok']} "
              f"hidden={row['hidden_ok']} {row['secs']}s", flush=True)
    print("\n| task | applied | own tests | hidden test | time |\n|---|---|---|---|---|")
    for r in rows:
        print(f"| {r['task']} | {r['applied']} | {r['suite_ok']} | {r['hidden_ok']} | {r['secs']}s |")
    print(f"\nsolved (hidden test passes): {sum(r['hidden_ok'] for r in rows)}/{len(rows)}")


if __name__ == "__main__":
    main()
