# Agent Guide for freshintelliventHacs

This guide is for agentic coding assistants working in this repository.
It captures how to build, lint, and test the codebase, plus local coding
conventions discovered from the repository.

## Repository Overview

- Home Assistant custom integration for Fresh Intellivent Sky.
- Python-only, integration lives under `custom_components/fresh_intellivent_sky_alt`.
- CI runs linting (Black + Flake8) and HACS validation.
- No tests or build steps are defined in this repo.

## Commands (Build, Lint, Test)

### Build

- No build step is configured.
- This repo is distributed as source for Home Assistant.

### Lint / Format

- CI installs Black and Flake8.
- Run locally from repo root:
  - `python -m black .`
  - `python -m flake8`
- Flake8 config lives in `setup.cfg`.
- Flake8 ignores: E501, W503, E203, D202, W504.

### Tests

- No automated test suite found in repo.
- If you add tests later (typical is pytest):
  - All tests: `pytest`
  - Single test file: `pytest path/to/test_file.py`
  - Single test function: `pytest path/to/test_file.py::test_name`
  - By keyword: `pytest -k "keyword"`

### HACS Validation

- CI runs the HACS GitHub Action.
- There is no local command specified; validation happens in CI.

## Cursor/Copilot Rules

- No `.cursorrules`, `.cursor/rules/*`, or `.github/copilot-instructions.md` found.

## Code Style Guidelines

### General Python Style

- Use Black formatting (default 88 char line length).
- Prefer `from __future__ import annotations` in modules.
- Use module docstrings as the first line in each Python file.
- Use f-strings for string formatting.
- Prefer list/tuple literals over manual `list()`/`tuple()`.

### Imports

- Follow this order (as seen in codebase):
  1. `__future__` imports
  2. Standard library
  3. Third-party / Home Assistant
  4. Local integration imports (`from .const import ...`)
- Use grouped imports and keep a blank line between groups.
- Prefer explicit imports over `import *`.

### Typing

- Use type hints for function signatures.
- Use Home Assistant typing helpers when available.
- Prefer explicit types in coordinator entities:
  - `DataUpdateCoordinator[FreshIntelliVent]` where applicable.
- Avoid `Any` unless necessary for external library types.

### Naming Conventions

- Constants: `UPPER_SNAKE_CASE` (see `const.py`).
- Classes: `PascalCase`.
- Functions and variables: `snake_case`.
- Module-level logger: `_LOGGER = logging.getLogger(__name__)`.
- Domain string: `DOMAIN = "fresh_intellivent_sky_alt"`.

### Async Patterns (Home Assistant)

- Prefer async I/O and Home Assistant async APIs.
- Use `async_setup_entry` and `async_unload_entry` for integration lifecycle.
- For periodic updates, use `DataUpdateCoordinator` and `UpdateFailed`.
- Keep BLE and network operations in async flows.

### Error Handling

- Use Home Assistant exceptions:
  - `ConfigEntryNotReady` for missing hardware at setup.
  - `HomeAssistantError` for integration-level issues.
  - `UpdateFailed` for coordinator update failures.
- Avoid broad exceptions unless required; when used, log or convert to
  HA-specific exceptions.
- Always attempt cleanup (disconnect) in `finally`-style flows.

### Logging

- Use `_LOGGER` for debug/error logs.
- Log failures in cleanup or disconnection steps.
- Avoid noisy logs in tight loops.

### Home Assistant Integration Patterns

- Store coordinator in `hass.data[DOMAIN][entry.entry_id]`.
- Use `async_forward_entry_setups` for platform setup.
- Use `EntityDescription` for entities (sensor, number, select, switch).
- Set `_attr_has_entity_name = True` on entities.
- Populate `DeviceInfo` for device registry entries.

### Constants and Configuration

- Centralize constants in `custom_components/fresh_intellivent_sky_alt/const.py`.
- For config options use constants like `CONF_AUTH_KEY`, `CONF_SCAN_INTERVAL`.
- Use `DEFAULT_SCAN_INTERVAL` and `TIMEOUT` from `const.py`.
- Keep config flow keys and user-facing strings consistent.

### Data Update and Device Access

- Use `FreshIntelliVent` client and BLE device from HA bluetooth helpers.
- Authenticate only if `auth_key` is present.
- Fetch sensor and device info before updating entities.
- Updates should populate shared data via `FetchAndUpdate`.

### JSON/Translation Files

- Translations live in `custom_components/fresh_intellivent_sky_alt/translations`.
- Keep keys stable; use lower_snake_case for translation keys.
- `strings.json` should mirror translation structure.

### Versioning and Metadata

- Integration metadata is defined in `manifest.json`.
- Bump version in `manifest.json` when releasing.
- Keep `requirements` pinned to explicit versions.

## Suggested Workflow for Agents

1. Read `custom_components/fresh_intellivent_sky_alt/` files relevant to change.
2. Update or add constants in `custom_components/fresh_intellivent_sky_alt/const.py`.
3. Ensure async patterns and Home Assistant conventions are respected.
4. Run `python -m black .` and `python -m flake8` if touching Python files.
5. Note: no tests currently exist; avoid claiming test results.

## Files of Interest

- `custom_components/fresh_intellivent_sky_alt/__init__.py`: integration setup.
- `custom_components/fresh_intellivent_sky_alt/config_flow.py`: UI config flow.
- `custom_components/fresh_intellivent_sky_alt/sensor.py`: sensor entities.
- `custom_components/fresh_intellivent_sky_alt/switch.py`: switch entities.
- `custom_components/fresh_intellivent_sky_alt/number.py`: number entities.
- `custom_components/fresh_intellivent_sky_alt/select.py`: select entities.
- `custom_components/fresh_intellivent_sky_alt/const.py`: constants.

## Notes for Changes

- Keep async methods non-blocking; avoid synchronous sleeps or I/O.
- Maintain consistent entity naming across platforms.
- Use Home Assistant entity categories (e.g., diagnostics) when applicable.
- Add new device properties to `DeviceInfo` only when available.
