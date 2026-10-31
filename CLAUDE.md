# Project instructions for Claude Code

Project: Autonomous Feral Pigeon Deterrent System. Owner: Arjun (strong in mechanical design, new to the software workflow, knows basic Python). The constitution at .specify/memory/constitution.md and docs/system-scope.md are the source of truth. If a request conflicts with them, stop and tell the owner.

## Report after EVERY prompt or task
When you finish, reply with a report in exactly this format. Build the file list from `git status --short` and `git diff --stat` run AFTER your changes. Never build it from memory.

### Summary
2-3 plain sentences: what you did and why.

### Files changed
| Path | Action (created / modified / deleted) | What changed | Why |
List every file, including generated and config files. If git shows a file that is not in this table, the report is wrong.

### Commands run
Each command and its result.

### Tests
What ran, what passed or failed, and what is not yet tested.

### Decisions and assumptions
Anything you chose that the owner did not specify. Flag anything uncertain.

### Not done / follow-ups

### How to verify (owner checklist)
Concrete manual steps with expected results.

## Working rules
- Do only what the prompt asks. One task at a time, then stop. Do not start the next task.
- Do not commit unless asked. When asked, use Conventional Commits.
- Never delete files, run destructive commands, install global tools or change security settings without asking first.
- Never read, print or commit secrets, credentials, .env files, raw camera footage or datasets.
- Explain code in plain language. The owner must be able to explain every module in an interview. This point is highly important
- Safety first: never write a code path that can fire the effector without every interlock in the constitution.
- If a requirement is ambiguous, ask. Do not guess silently.
- Environment: Windows 11, PowerShell, Python managed with uv.