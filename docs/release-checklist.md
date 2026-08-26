# Release Checklist

Use this before publishing a GitHub release.

## Clean Working Tree

```powershell
git status --short
```

The output should not include caches, compiled Python files, local configs, logs, sessions, support bundles, dumps, ETL files, or private machine data.

## Tests

```powershell
python -m pip install -e .
python -m unittest discover -v
gpu-flight-recorder verify-events --days 0
```

The verify command should start, print event query health, and exit without creating a recording session.

## Public Safety Check

```powershell
rg -n "C:\\Users\\|OneDrive\\|AppData\\|Minidump|Minidumps|LiveKernelReports|PRIVATE|SECRET|TOKEN|PASSWORD|API_KEY|Puffy|PolyForm" .
```

Expected public examples such as `C:\Users\You` or documented Windows paths are fine. Real private paths, secrets, dumps, session logs, support ZIPs, or leftover private author/license tokens are not.

## License and Wording

- `LICENSE` is present and is the MIT License.
- Copyright line names the intended author.
- README, `pyproject.toml`, and `LICENSE` all agree on MIT.
- GitHub owner placeholders in `README.md` and `pyproject.toml` have been replaced with the real GitHub owner.

## GitHub Release

```powershell
git tag v0.1.0-beta.1
```

Create a GitHub release from the tag and mark it as a pre-release.

Suggested title:

```text
GPU Flight Recorder 0.1.0 beta 1
```

Suggested notes:

```text
First public beta of GPU Flight Recorder, a local Windows diagnostic recorder for GPU/display freezes and driver-path stalls.

Released under the MIT License. It is a diagnostic tool, not a repair tool, and it is a portfolio-quality beta.
```
