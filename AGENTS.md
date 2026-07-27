# Agent Quick Configuration

This repository is an ASMR-focused media archive. It discovers provider-backed
origins from YouTube, Twitch, and RSS, stores provider-neutral state in SQLite,
downloads media with `yt-dlp`, and can deliver artifacts to Telegram.

## Start here

1. Run `git status --short --branch` before editing. Preserve unrelated local
   changes and never use broad staging in a dirty checkout.
2. Read `README.md` for user-facing behavior and `SKILL.md` for the full code
   map, invariants, and operational notes.
3. The public project and CLI name is `asmr-tg-backup`. The Python import
   namespace remains `ytb_tg_backup` for compatibility.

## Safe local setup

```bash
python3 -m venv .venv
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests
```

Create a local runtime config only when the task needs it:

```bash
cp config.example.toml config.toml
chmod 600 config.toml
```

Before running `init`, point `[app].data_dir` at a dedicated temporary or
explicitly approved runtime directory. Do not reuse the live service config for
development checks.

The runtime package has no required Python dependencies. An editable install is
optional:

```bash
.venv/bin/pip install -e .
asmr-tg-backup status --config config.toml
```

If the thin virtual environment cannot import `setuptools.build_meta`, keep
using `PYTHONPATH=src`; do not download dependencies merely to run the tests.

## Guardrails

- Never commit `config.toml`, environment files, tokens, chat IDs, SQLite
  files, downloads, archives, or generated observer reports.
- Tests must mock provider APIs, Telegram calls, and media subprocesses.
- Do not run real polling, downloading, transcoding, Telegram delivery, or
  authenticated/member observation unless the user explicitly requests it.
- New source behavior belongs in provider-backed `Origin` adapters. Preserve
  namespaced media identity, per-origin associations, lease-token checks,
  independent download/delivery failure budgets, and uncertain-delivery safety.
- Telegram control changes must preserve user/chat/thread authorization,
  active message ID checks, expiry, and `panel_revision` validation.
- Treat any local member-observer code, service/config, and generated reports
  as an isolated experiment unless the user explicitly puts it in scope.

## Validation

Run the smallest relevant test module while editing, then the full suite:

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_control
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests
```

For release or runtime work, stage only enumerated files, verify the clean
staged tree, and check the user service after restart. Do not create a GitHub
Release page or upload artifacts unless the user asks.
