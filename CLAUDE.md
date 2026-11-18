# Claude Code instructions

Claude Code primarily implements approved tasks and addresses Codex review findings within the approved scope. Return findings that require a scope change to the owner for a decision.

## Scope and approval

- Use [docs/PLAN.md](docs/PLAN.md) as the source of truth for scope, task order, acceptance criteria, and progress. Read the relevant sections and existing code before working.
- Reading, investigation, and existing local checks may begin immediately. Consult [the baseline report](docs/REPORT_v1.md) before repeating completed checks. Read, import, or follow archived developer instructions only if the owner explicitly requests it.
- Implementation requires the owner’s explicit approval of a specific task or clearly defined scope. A task listed next in the plan is awaiting assignment. Once approved, complete the work and its verification without asking again for routine actions.
- Preserve existing user changes, including staged and unstaged work. Obtain separate approval for unrelated features or refactors, dependency changes, or material plan changes. Commit, push, or deploy only with explicit authorization.

## Verification and handoff

- Use local services, synthetic fixtures, and disposable databases. Keep Plaid credentials out of the test environment. Never print secrets or environment variable values.
- Run the checks required by the task. From the repository root, `python -m pytest` runs the test suite. Run `python tests/check_baseline.py` separately for the render and structure checks.
- Report failures and skipped checks honestly. Never weaken a check or rewrite a baseline to conceal a failure.
- After implementation, give the owner and Codex a handoff covering changes, verification performed and its results, limitations, and remaining issues. Identify any acceptance criterion still unverified.
