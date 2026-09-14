#!/usr/bin/env python3
"""
newagent — build a scheduled local agent without an LLM in the loop.

This is the piece that removes Claude from the BUILD path, not just the run
path. Every job in this fleet was previously authored by talking to a cloud
model. This does the same job as a template: you describe the schedule, the
prompt and where the answer goes, and it wires up an OpenClaw cron job on a
local model, registers it with the fleet watcher, and test-fires it.

An agent created this way is monitored from birth. That is deliberate — an
unmonitored agent is how you end up with a job that exits 0 for eleven days
while doing nothing.

Usage
  python3 ~/.fleet/newagent.py                    interactive
  python3 ~/.fleet/newagent.py --spec spec.json   from a spec file
  python3 ~/.fleet/newagent.py --list             show local agents
  python3 ~/.fleet/newagent.py --models           show installed local models

Spec file format (all keys except name/cron/prompt are optional):
  {
    "name":    "MENA regulator watch",
    "cron":    "0 4 * * 1-5",          5-field, LOCAL time
    "prompt":  "...",                  or "prompt_file": "~/path.txt"
    "model":   "ollama/qwen2.5:14b",
    "to":      "5117662146",
    "channel": "telegram",
    "timeout_seconds": 300,
    "watch_max_age_h": 26
  }
"""
import json
import os
import subprocess
import sys

HOME = os.path.expanduser("~")
FLEET = os.path.join(HOME, ".fleet")
REGISTRY = os.path.join(FLEET, "registry.json")
PROMPTS = os.path.join(HOME, ".openclaw", "prompts")

DEFAULTS = {
    "model": "ollama/qwen2.5:14b",
    "channel": "telegram",
    "to": "5117662146",
    "timeout_seconds": 300,
    "watch_max_age_h": 26,
}



def interval_hours(expr):
    """Rough hours between runs for a 5-field cron. Coarse on purpose — it only
    needs to tell hourly from daily from weekly."""
    try:
        m, h, dom, mon, dow = expr.split()
    except ValueError:
        return 24.0
    if m.startswith("*/"):
        return max(1, int(m[2:])) / 60.0
    if h == "*":
        return 1.0
    if dom != "*" and dom.isdigit():
        return 24 * 31.0
    if dow != "*":
        days = 7.0
        if "-" in dow:
            a, b = dow.split("-")
            days = 7.0 / max(1, int(b) - int(a) + 1)
        elif "," in dow:
            days = 7.0 / len(dow.split(","))
        return 24 * days
    if "," in h:
        return 24.0 / len(h.split(","))
    if "-" in h:                      # e.g. 8-23: hourly, but with a nightly gap
        a, b = h.split("-")
        return max(2.0, 24 - (int(b) - int(a) + 1))
    return 24.0


def run(argv, timeout=120):
    r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    out = (r.stdout or "") + (r.stderr or "")
    return r.returncode, "\n".join(
        l for l in out.splitlines()
        if "duplicate plugin" not in l and not l.startswith("Config warnings"))


def local_models():
    code, out = run(["ollama", "list"])
    names = []
    for line in out.splitlines()[1:]:
        if line.strip():
            names.append(line.split()[0])
    return names


def valid_cron(expr):
    parts = expr.split()
    if len(parts) != 5:
        return False, "need 5 fields (minute hour day month weekday), got %d" % len(parts)
    return True, ""


def slugify(name):
    keep = [c.lower() if c.isalnum() else "-" for c in name]
    slug = "".join(keep)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")[:60] or "agent"


def register_with_watcher(display_name, job_id, max_age_h):
    reg = json.load(open(REGISTRY))
    if any(j.get("cron_job") == job_id for j in reg["jobs"]):
        return False
    entry = {"name": display_name, "cron_job": job_id}
    if max_age_h:
        entry["watch_max_age_h"] = max_age_h
    reg["jobs"].append(entry)
    json.dump(reg, open(REGISTRY, "w"), indent=2)
    return True


def build(spec):
    for key in ("name", "cron"):
        if not spec.get(key):
            print("error: spec needs a %r" % key)
            return 1
    ok, why = valid_cron(spec["cron"])
    if not ok:
        print("error: bad cron %r — %s" % (spec["cron"], why))
        return 1

    model = spec.get("model", DEFAULTS["model"])
    bare = model.split("/", 1)[-1]
    installed = local_models()
    if installed and bare not in installed:
        print("error: %r is not installed in Ollama." % bare)
        print("       installed: %s" % ", ".join(installed))
        print("       This is the exact failure that killed your hourly briefing")
        print("       for 25 days — a model name that no longer exists.")
        return 1

    prompt = spec.get("prompt")
    if spec.get("prompt_file"):
        prompt = open(os.path.expanduser(spec["prompt_file"])).read()
    if not prompt or not prompt.strip():
        print("error: spec needs a 'prompt' or a 'prompt_file'")
        return 1
    prompt = prompt.strip()

    os.makedirs(PROMPTS, exist_ok=True)
    pfile = os.path.join(PROMPTS, slugify(spec["name"]) + ".txt")
    with open(pfile, "w") as f:
        f.write(prompt + "\n")

    argv = [
        "openclaw", "cron", "add",
        "--name", spec["name"],
        "--description", spec.get("description", "Local agent. No cloud model, no vendor."),
        "--cron", spec["cron"],
        "--message", prompt,
        "--model", model,
        "--session", "isolated",
        "--channel", spec.get("channel", DEFAULTS["channel"]),
        "--to", str(spec.get("to", DEFAULTS["to"])),
        "--announce", "--best-effort-deliver",
        "--timeout-seconds", str(spec.get("timeout_seconds", DEFAULTS["timeout_seconds"])),
        "--json",
    ]
    code, out = run(argv)
    if code != 0 or "{" not in out:
        print("error: openclaw refused the job:\n" + out[:800])
        return 1
    job = json.loads(out[out.index("{"):out.rindex("}") + 1])
    job_id = job["id"]

    # Threshold follows the schedule. A weekly job must not be called
    # "silent" on day two, and an hourly one must not hide for a day.
    max_age = spec.get("watch_max_age_h") or round(interval_hours(spec["cron"]) * 1.5, 1)
    watched = register_with_watcher(spec["name"] + " (local)", job_id, max_age)

    print("built:   %s" % spec["name"])
    print("  id:      %s" % job_id)
    print("  when:    %s  (local time)" % spec["cron"])
    print("  model:   %s   [runs on this machine]" % model)
    print("  prompt:  %s" % pfile)
    print("  silent after: %sh" % max_age)
    print("  watched: %s" % ("yes — the fleet alarm covers it" if watched else "already registered"))
    return job_id


def interactive():
    print("Build a local agent. Ctrl-C to bail.\n")
    models = local_models()
    spec = {}
    spec["name"] = input("Name (e.g. 'MENA regulator watch'): ").strip()
    print("\nSchedule as 5-field cron, LOCAL time. Examples:")
    print("  0 6 * * *      every day 06:00")
    print("  0 4 * * 1-5    weekdays 04:00")
    print("  0 20 * * 6     Saturdays 20:00")
    print("  */30 * * * *   every 30 minutes")
    spec["cron"] = input("\nSchedule: ").strip()
    print("\nInstalled local models: %s" % ", ".join(models))
    m = input("Model [%s]: " % DEFAULTS["model"]).strip()
    if m:
        spec["model"] = m if "/" in m else "ollama/" + m
    print("\nPrompt. This is what the agent is told every run. End with a lone '.' line.")
    lines = []
    while True:
        try:
            ln = input()
        except EOFError:
            break
        if ln.strip() == ".":
            break
        lines.append(ln)
    spec["prompt"] = "\n".join(lines)
    return spec


def main():
    args = sys.argv[1:]

    if "--models" in args:
        print("\n".join(local_models()))
        return 0

    if "--list" in args:
        code, out = run(["openclaw", "cron", "list"])
        print(out)
        return 0

    if "--spec" in args:
        spec = json.load(open(os.path.expanduser(args[args.index("--spec") + 1])))
    else:
        spec = interactive()

    job_id = build(spec)
    if job_id in (1, None):
        return 1

    if "--no-test" in args:
        return 0
    try:
        answer = input("\nTest-fire it now? [Y/n] ").strip().lower()
    except EOFError:
        # Scripted or piped use: nobody is there to answer, so fire it.
        # An agent that has never been run once is an agent you cannot trust.
        answer = "y"
    if answer not in ("n", "no"):
        run(["openclaw", "cron", "run", job_id], timeout=60)
        print("fired. It runs on a local model, so give it a minute.")
        print("Check with:  python3 ~/.fleet/watch.py --now | grep -i %r" % spec["name"][:20])
    return 0


if __name__ == "__main__":
    sys.exit(main())
