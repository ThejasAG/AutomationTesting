# Vendored: archify

Upstream: https://github.com/tt-a1i/archify (MIT). Pinned commit in `.pinned-commit`.

Only the runtime skill directory is vendored — `examples/`, `test/` and `scripts/` are
dropped (3.5M + 1.1M + 48K of fixtures and build tooling we never invoke). The CLI uses
node builtins only, so there is nothing to `npm install`.

Used by `automation/workflow/archify_ir.py` (spine -> JSON IR) and
`automation/workflow/archify_cli.py` (render / compare).

To update: re-clone upstream, rsync the same subset, refresh `.pinned-commit`, and run
`pytest tests/test_archify_ir.py` — the golden IR test will catch a schema change.
