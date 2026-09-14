# Contributing

## Run the tests

```bash
python3 -m pip install pytest
python3 -m pytest tests/ -v      # 25 unit tests, no side effects
python3 selftest.py              # end-to-end, macOS, builds and removes its own fixtures
```

CI runs the unit tests on Python 3.9, 3.11 and 3.13. `selftest.py` needs a real
Mac with Docker and so is run by hand.

## The two rules

**1. Detection lives in one place.** `medic.py` imports `watch.check()`. If you
add a signal, add it there and both halves get it. A second opinion about
health is a second opinion that will drift.

**2. A remedy must be something you would be happy to have run unattended at
4am, three times, and then stop.** If that makes you hesitate, it is an
`escalate`, not an action.

## Adding a signal

1. Write the probe as a standalone function in `watch.py` that returns
   `True` / `False` / `None`. `None` means *the checker could not answer* — it
   is not the same as a failure, and the caller treats it differently.
2. Add the branch to `check()`, reading its config from the job dict.
3. Document the field in `docs/registry-schema.md`.
4. Add a job using it to `registry.example.json` — the test suite asserts every
   example job is valid and checkable.
5. Add a test. Probes take their input from the job dict, so they test without
   touching the network or the machine.

## Adding a remedy action

1. Write `act_<name>(args) -> (ok: bool, detail: str)` in `medic.py`.
2. Register it in `ACTIONS`. The registry can only *name* an action — it can
   never supply a command. Keep it that way: no action may take a shell string,
   a command list, or a path without an allow-list check.
3. Add it to the table in `docs/registry-schema.md` and the README.
4. Add a guardrail test. `test_the_action_set_is_closed` will fail until you
   update it, which is intentional — adding an action should be a deliberate,
   visible act.

## Things that will get a PR sent back

- A check that reports `ok` when it could not actually determine health.
- A remedy with no attempt ceiling, or one that retries on its own inside the
  action.
- A remedy that acts on an unconfirmed problem.
- Anything that makes `watch.py` write to the things it watches. It is
  read-only, and that separation is the reason it can be trusted.
- Config that lets a registry file execute arbitrary code.

## Reporting a problem

Include the output of:

```bash
python3 watch.py --now
python3 medic.py --status
```

Neither contains credentials. Both contain your job names and hostnames — skim
before pasting.
