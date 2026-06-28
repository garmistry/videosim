# Work Log

## 2026-06-28

- Added dependency installer for Linux and macOS at `scripts/install-deps.sh`.
- Added repository rule to commit successful completions and keep this work log updated.
- Completed Milestone 0 product-contract docs: `README.md`, `TEST_PLAN.md`, `ACCEPTANCE_MATRIX.md`, `TEST_GAPS.md`, `KNOWN_LIMITATIONS.md`, and `RUNBOOK.md`.
- Added `tests/test_docs_contract.py` to enforce critical requirement/test mapping and required feed mode behavior mapping.
- Added Python cache ignores for repeatable local test runs.
- Validation run: `python3 -m unittest tests.test_docs_contract` passed, 4 tests.
- Skipped checks: no live SRT, media, GUI, or Docker checks exist yet; those are blocked by later milestone implementation.
