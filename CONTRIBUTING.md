# Contributing

Noyi is a personal learning project. Issues and small pull requests are welcome, but replies may be slow and there is no roadmap commitment.

## Setup

Follow the install steps in the [README](README.md). The tests do not need an API key and do not call any model.

```bash
python -m pytest -q
npm test --prefix src/personal_agent/pi_bridge
```

## Ground rules

- **Fix the mechanism, not the example.** Don't add keyword lists, special cases, or hard-coded wording to make one failing case pass. Find what produces that class of problem and fix it there.
- **Show the failure first.** A bug fix or behaviour change should come with a test that fails before the change and passes after it.
- **Keep the ledger honest.** Nothing may report "done" without evidence. Unknown values stay unknown and are never estimated.
- **No secrets, no real personal data.** Use environment variable names and placeholders. Screenshots must come from the demo database (`scripts/demo-replay.py`), never from a personal one.
- Commit messages use Conventional Commit prefixes (`feat:`, `fix:`, `docs:`, ...).

New tools go in as capability plugins. See [module interfaces](docs/module-interfaces.md).
