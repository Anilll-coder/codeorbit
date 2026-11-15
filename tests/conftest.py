"""Make CLI output plain and unwrapped, whatever environment the tests run in.

Set before any test module imports typer or rich, because both read these once:

- typer forces terminal mode when GITHUB_ACTIONS, FORCE_COLOR or PY_COLORS is
  set, so in CI `--help` comes back full of ANSI codes and "--path" is split by
  escape sequences. _TYPER_FORCE_DISABLE_TERMINAL is typer's own off switch.
- rich wraps at 80 columns when there is no terminal. A long temp path (as on
  Linux runners) then pushes a line break into the middle of asserted phrases.
"""
import os

os.environ["_TYPER_FORCE_DISABLE_TERMINAL"] = "1"
os.environ["COLUMNS"] = "240"
os.environ["TERMINAL_WIDTH"] = "240"
for var in ("FORCE_COLOR", "PY_COLORS"):
    os.environ.pop(var, None)
