# AGENTS.md

Blast Radius: C++ PR semantic risk analyzer (pure Python, hackathon project). Tree-sitter C++ parsing + dual-LLM pipeline.

## Setup

- Install deps: `pip install -r requirements.txt`
- Requires recent py-tree-sitter (>=0.22): code calls `Parser(Language(...))`.
- Python 3.11 (CI version).
- LLM keys via env or `.env`: `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `OPENAI_API_KEY`, `NVIDIA_API_KEY` / `NEMOTRON_API_KEY` / `NVIDIA_NIM_API_KEY`. Missing keys are not an error — the pipeline returns callgraph-only output labeled **DEGRADED MODE**.

## Commands (run from repo root)

- Full pipeline: `python cli/blast_radius.py analyze <repo-path> <pr-diff-file> [--json] [--token-budget 8000] [--confidence-threshold 50] [--risk-model ...] [--crosscheck-model ...]` (or `python blast-radius.py ...`). Must run from repo root — modules import as `core.*` / `observability.*`.
- Web UI: `streamlit run ui/app.py` (from repo root) — analyzes a GitHub PR URL end-to-end; clones PR head into `.blast_radius/ui_clones/<owner>-<repo>-<pr>/` (gitignored, cached across runs). Public repos only unless `GITHUB_TOKEN` is set.
- **Unit tests:** `python -m pytest tests/ -q` — covers `diff_parser`, `callgraph`, `confidence`, `degrade` (simulated API failure).
- **De facto smoke tests:** modules *without* pytest coverage have a `__main__` block — run the ones you touched: `python -m core.context_slicer`, `python -m core.risk_model`, `python -m core.crosscheck_model`, `python -m observability.cost_latency_log`. Smoke tests write/delete `scratch_test/` (gitignored).
- Offline eval benchmark: `python -m eval.eval_harness` — MOCK mode: risks are derived from `ground_truth.json`, so F1 ≈ 1.0 always; metrics are structural only. Pass `--live` (requires an LLM API key, exits 1 without one) to run real models and get meaningful P/R/F1.
- Regenerate the 10-case benchmark suite: `python -m eval.generate_eval_suite` (writes `eval/test_cases/`).

## Architecture

Pipeline order: `core/diff_parser.py` (maps diff lines → enclosing C++ functions via tree-sitter) → `core/callgraph.py` (static 2-hop callers/callees, zero LLM) → `core/context_slicer.py` (token-budgeted bundle) → `core/risk_model.py` (primary LLM, strict JSON) → `core/crosscheck_model.py` (independent audit LLM) → `core/confidence.py` (score 0–100; below threshold ⇒ "NEEDS HUMAN REVIEW"). `core/degrade.py` wraps all LLM execution and falls back to static-only analysis on any API failure. Every LLM call is logged to `observability/logs.jsonl` via `observability/cost_latency_log.py`.

## Gotchas

- **Model fallback** (`core/risk_model.py`, `core/crosscheck_model.py`): a `claude-*` request without `ANTHROPIC_API_KEY` switches provider automatically — `gemini/gemini-2.5-flash` (runs via the direct `google.generativeai` SDK, not litellm) → NVIDIA NIM (`openai/meta/llama-3.3-70b-instruct`) → `gpt-4o` (crosscheck: `gpt-4o-mini`). A warning is printed to stderr; check the `model` field in output to see what actually ran.
- LLM responses must be strict JSON; markdown fences are stripped by `clean_json_response`. Schema violations are coerced (bad severity → medium), not rejected.
- New eval cases go in `eval/test_cases/<case_name>/` with `pr.diff`, `repo/src/*.cpp`, and `ground_truth.json` (`has_contract_break`, legacy alias `has_regression`, plus `expected_risks`). Regenerate the whole suite with `python -m eval.generate_eval_suite`.
- CI (`.github/workflows/blast-radius.yml`) dogfoods this tool on every PR of this repo: generates `pr.diff` against the base branch, analyzes `./`, posts/updates a PR comment. Analyzer failure does not fail CI (`|| true`). The comment step is skipped on fork PRs (fork workflow tokens are read-only ⇒ 403) and has `continue-on-error`.
- `*.jsonl`, `.env`, `scratch*/`, and `.blast_radius/` are gitignored — never commit logs, keys, or UI clone caches.
