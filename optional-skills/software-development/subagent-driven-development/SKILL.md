---
name: subagent-driven-development
description: "Execute plans via delegate_task subagents (2-stage review)."
version: 1.1.0
author: Hermes Agent (adapted from obra/superpowers)
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [delegation, subagent, implementation, workflow, parallel]
    related_skills: [requesting-code-review, test-driven-development]
---

# Subagent-Driven Development

## Overview

Execute implementation plans with selective delegation: use subagents when tasks are genuinely separable or independent review materially reduces risk, and keep cohesive/local work in the controller when delegation would only add overhead.

**Core principle:** Delegate for clear parallelism, isolation, or review value — not because every task needs a fresh implementer and two reviewers.

## When to Use

Use this skill when:
- You have a multi-part implementation where tasks can be separated cleanly
- Parallel investigation/implementation will save time without creating merge conflicts
- A security, data-integrity, API, or other high-risk change benefits from independent review
- A focused subagent can work from a bounded context more effectively than the controller

Prefer direct execution for small or tightly coupled changes, single-file fixes, and work where the controller already has the necessary context. A user request to implement is already authorization to proceed within scope; delegation must not create a second approval gate.

**Delegation benefit:**
- Fresh context can isolate a bounded task
- Independent review can catch concrete high-risk defects
- Parallel tasks can reduce wall-clock time when they do not touch the same state

## The Process

### 1. Read and Parse Plan

Read the plan file. Extract ALL tasks with their full text and context upfront. Create a todo list:

```python
# Read the plan
read_file("docs/plans/feature-plan.md")

# Create todo list with all tasks
todo([
    {"id": "task-1", "content": "Create User model with email field", "status": "pending"},
    {"id": "task-2", "content": "Add password hashing utility", "status": "pending"},
    {"id": "task-3", "content": "Create login endpoint", "status": "pending"},
])
```

**Key:** Read the plan ONCE. Extract everything. Don't make subagents read the plan file — provide the full task text directly in context.

### 2. Per-Task Workflow

For each task you choose to delegate (not every task must be delegated):

#### Step 1: Dispatch Implementer Subagent

Use `delegate_task` with complete context:

```python
delegate_task(
    goal="Implement Task 1: Create User model with email and password_hash fields",
    context="""
    TASK FROM PLAN:
    - Create: src/models/user.py
    - Add User class with email (str) and password_hash (str) fields
    - Use bcrypt for password hashing
    - Include __repr__ for debugging

    FOLLOW TDD:
    1. Write failing test in tests/models/test_user.py
    2. Run: pytest tests/models/test_user.py -v (verify FAIL)
    3. Write minimal implementation
    4. Run: pytest tests/models/test_user.py -v (verify PASS)
    5. Run: pytest tests/ -q (verify no regressions)
    6. Commit only task files: git add src/models/user.py tests/models/test_user.py && git commit -m "feat: add User model with password hashing"

    PROJECT CONTEXT:
    - Python 3.11, Flask app in src/app.py
    - Existing models in src/models/
    - Tests use pytest, run from project root
    - bcrypt already in requirements.txt
    """,
    toolsets=['terminal', 'file']
)
```

#### Step 2: Dispatch Spec Compliance Reviewer

After the implementer completes, verify against the original spec:

```python
delegate_task(
    goal="Review if implementation matches the spec from the plan",
    context="""
    ORIGINAL TASK SPEC:
    - Create src/models/user.py with User class
    - Fields: email (str), password_hash (str)
    - Use bcrypt for password hashing
    - Include __repr__

    CHECK:
    - [ ] All requirements from spec implemented?
    - [ ] File paths match spec?
    - [ ] Function signatures match spec?
    - [ ] Behavior matches expected?
    - [ ] Nothing extra added (no scope creep)?

    OUTPUT: PASS or list of specific spec gaps to fix.
    """,
    toolsets=['file']
)
```

Use a separate spec reviewer when requirements are complex, ambiguous, or high-risk. For routine tasks, the controller may compare the diff against the task directly. If a reviewer finds a **specific** gap, fix it and re-review the affected area; do not open another review round when no defect was found.

#### Step 3: Dispatch Code Quality Reviewer

After spec compliance passes:

```python
delegate_task(
    goal="Review code quality for Task 1 implementation",
    context="""
    FILES TO REVIEW:
    - src/models/user.py
    - tests/models/test_user.py

    CHECK:
    - [ ] Follows project conventions and style?
    - [ ] Proper error handling?
    - [ ] Clear variable/function names?
    - [ ] Adequate test coverage?
    - [ ] No obvious bugs or missed edge cases?
    - [ ] No security issues?

    OUTPUT FORMAT:
    - Critical Issues: [must fix before proceeding]
    - Important Issues: [should fix]
    - Minor Issues: [optional]
    - Verdict: APPROVED or REQUEST_CHANGES
    """,
    toolsets=['file']
)
```

Use a separate quality reviewer when change risk justifies it. If concrete critical/important issues are found, fix and re-review the affected area. Stop when the identified issues are resolved; do not repeat review simply to obtain another approval.

#### Step 4: Mark Complete

```python
todo([{"id": "task-1", "content": "Create User model with email field", "status": "completed"}], merge=True)
```

### 3. Final Review

For cross-cutting, high-risk, or independently implemented work, an integration reviewer can be useful after all tasks are complete. For a small cohesive change, verify integration directly and skip a redundant reviewer:

```python
delegate_task(
    goal="Review the entire implementation for consistency and integration issues",
    context="""
    All tasks from the plan are complete. Review the full implementation:
    - Do all components work together?
    - Any inconsistencies between tasks?
    - All tests passing?
    - Ready for merge?
    """,
    toolsets=['terminal', 'file']
)
```

### 4. Verify and Commit

```bash
# Run tests relevant to the changed behavior; use the full suite when project/risk requires it
pytest tests/path/to/affected_tests.py -q

# Review all task changes
git diff --stat

# Commit only the files belonging to this logical task
git add path/to/changed_file path/to/test_file && git commit -m "feat: complete [feature name] implementation"
```

## Task Granularity

Size tasks around coherent, reviewable deliverables. Avoid both giant ambiguous tasks and artificial 2–5 minute fragmentation that creates needless handoffs.

**Too big:**
- "Implement user authentication system"

**Right size:**
- "Create User model with email and password fields"
- "Add password hashing function"
- "Create login endpoint"
- "Add JWT token generation"
- "Create registration endpoint"

## Red Flags — Never Do These

- Delegate tightly coupled tasks to multiple agents editing the same files concurrently
- Proceed with known critical/important issues
- Give a subagent too little context to understand its bounded task
- Ignore a concrete reviewer finding without resolving or explicitly accepting the risk
- Re-run reviewers when no defect was found merely to obtain another approval
- Stage or commit unrelated files while completing a delegated task

## Handling Issues

### If Subagent Asks Questions

- Answer clearly and completely
- Provide additional context if needed
- Don't rush them into implementation

### If Reviewer Finds Issues

- The implementer or controller fixes the specific issues; a new fixer is optional, not mandatory
- Re-review the affected area when the issue is material
- Bound fix-and-reverify loops (normally at most two); if a concrete defect remains, escalate instead of looping indefinitely

### If Subagent Fails a Task

- Fix locally in the controller when the issue is small and context is already available, or dispatch a new focused subagent when isolation would help
- Preserve the original task scope; do not spawn replacement agents reflexively

## Efficiency Notes

**When fresh subagents help:**
- A task has a clean boundary and benefits from isolated context
- Several tasks can proceed independently without touching the same state
- A high-risk change benefits from a genuinely independent reviewer

**When review layers help:**
- Spec review catches material scope/compliance gaps on complex requirements
- Quality/security review can catch defects on risky changes

Every extra agent has cost and coordination overhead. Use only the layers that add evidence for the current task.

## Integration with Other Skills

### With plan

This skill EXECUTES plans created by the `plan` skill:
1. User requirements → plan → implementation plan
2. Implementation plan → subagent-driven-development → working code

### With test-driven-development

For behavioral logic and bug fixes, use appropriate regression/TDD guidance from `test-driven-development`. Low-impact docs, style, configuration, or already-correct implementation should use the verification that fits the change instead of forcing a RED/GREEN ritual.

### With requesting-code-review

Use `requesting-code-review` when an independent review is warranted by risk or explicitly requested; do not automatically run it after every delegated task.

### With systematic-debugging

If a subagent encounters bugs during implementation:
1. Follow systematic-debugging process
2. Find root cause before fixing
3. Write regression test
4. Resume implementation

## Example Workflow

```
[Read plan: docs/plans/auth-feature.md]
[Create todo list with 5 tasks]

--- Task 1: Create User model ---
[Dispatch implementer subagent]
  Implementer: "Should email be unique?"
  You: "Yes, email must be unique"
  Implementer: Implemented, 3/3 tests passing, committed.

[Dispatch spec reviewer]
  Spec reviewer: ✅ PASS — all requirements met

[Dispatch quality reviewer]
  Quality reviewer: ✅ APPROVED — clean code, good tests

[Mark Task 1 complete]

--- Task 2: Password hashing ---
[Dispatch implementer subagent]
  Implementer: No questions, implemented, 5/5 tests passing.

[Dispatch spec reviewer]
  Spec reviewer: ❌ Missing: password strength validation (spec says "min 8 chars")

[Implementer fixes]
  Implementer: Added validation, 7/7 tests passing.

[Dispatch spec reviewer again]
  Spec reviewer: ✅ PASS

[Dispatch quality reviewer]
  Quality reviewer: Important: Magic number 8, extract to constant
  Implementer: Extracted MIN_PASSWORD_LENGTH constant
  Quality reviewer: ✅ APPROVED

[Mark Task 2 complete]

... (continue for all tasks)

[After all tasks: dispatch final integration reviewer]
[Run full test suite: all passing]
[Done!]
```

## Remember

```
Fresh subagent per task
Two-stage review every time
Spec compliance FIRST
Code quality SECOND
Never skip reviews
Catch issues early
```

**Quality is not an accident. It's the result of systematic process.**

## Further reading (load when relevant)

When the orchestration involves significant context usage, long review loops, or complex validation checkpoints, load these references for the specific discipline:

- **`references/context-budget-discipline.md`** — Four-tier context degradation model (PEAK / GOOD / DEGRADING / POOR), read-depth rules that scale with context window size, and early warning signs of silent degradation. Load when a run will clearly consume significant context (multi-phase plans, many subagents, large artifacts).
- **`references/gates-taxonomy.md`** — The four canonical gate types (Pre-flight, Revision, Escalation, Abort) with behavior, recovery, and examples. Load when designing or reviewing any workflow that has validation checkpoints — use the vocabulary explicitly so each gate has defined entry, failure behavior, and resumption rules.

Both references adapted from gsd-build/get-shit-done (MIT © 2025 Lex Christopherson).
