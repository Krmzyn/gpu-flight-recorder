# Contributing

Thanks for considering a contribution.

GPU Flight Recorder is source-available under the PolyForm Noncommercial License 1.0.0. By submitting a contribution, you agree that your contribution can be used, modified, and distributed under that same license, and may be included in future versions of the project.

## Ground Rules

- Do not include private logs, support bundles, crash dumps, machine identifiers, or personal paths in issues or pull requests.
- Keep changes focused. This tool is intentionally local, small, and Windows-focused.
- Preserve the manual GUI flow unless the issue is explicitly about another runner mode.
- Do not add runtime dependencies without a clear reason.
- Do not call this project open source; the license is noncommercial source-available.

## Development

```powershell
python -m pip install -e .
python -m unittest discover -v
```

## Pull Requests

Before opening a pull request:

- run the test suite
- check that no `__pycache__`, `*.pyc`, logs, sessions, local configs, or ZIP support bundles are included
- update README or docs when behavior changes
- describe the diagnostic problem the change solves
