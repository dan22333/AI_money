.PHONY: test eval

# Fast, deterministic unit/integration suite (no model calls). Runs in CI.
test:
	cd agent && uv run pytest -q

# Agent behavior evals — REAL model, nondeterministic, on-demand (not PR CI).
# Needs OPENROUTER_API_KEY. See agent/evals/.
eval:
	cd agent && uv run python -m evals.run
