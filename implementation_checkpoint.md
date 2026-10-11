# r49 implementation checkpoint

## Phase 1 — scope and official source
- Worktree /home/ubuntu/projects/hermes-r49-context-loop; branch vps-r49-context-loop.
- Base 76f8c3b3c06b4d16e55a78a39e1c1e17a678c0b6; official origin/main 1fd18277267152893a8a24792b79213e7ca3f899 (shared Git database; no fetch).
- Read agent/AGENTS.md and tools/AGENTS.md. No production/container/config/Docker actions.
- Ported profile/provider cache eviction verbatim; ported explicit_auth_with_task_chain gate and task-chain-only quarantine retry. Kept baseline retry count and unrelated upstream behavior.

## Phase 2 — scoped path guard
- Added capped per-task/profile/path state for host-backed files <=200 lines and <=32 KiB, tracking actual returned line coverage and stat version.
- Six futile reads produce stable stalled_path status independent of pagination/unrelated calls. New lines/version/writes reset; compaction grants one recovery read per path.
- Agent halts after three stalled_path results for the same version even with general hard stops disabled. No general api_server hard-stop or AGY cap changes.
- Remote/container paths are excluded from host stat inference. No duplicate execution or sends introduced.

## Phase 3 — reproduction and initial verification
- New behavior tests on unpatched HEAD: four fail, one passes (unconfigured fallback remains prohibited); exact sources restored immediately after baseline reproduction.
- Patched direct .venv/bin/python -m pytest targeted/relevant regressions: 88 passed in 16.12s.
- Initial ruff check and py_compile passed. Final isolated runner includes auxiliary-client, runtime-cache, guardrail and file-state regressions.
- Review found preserved-mtime edits need region cache invalidation on ctime/size/inode version changes; added this and a regression assertion before final verification.

## Phase 4 — regression review
- Broad isolated run: 306 passed; failures exposed two stale refresh fixtures/contracts and one missing optional dependency.
- Updated existing refresh tests to use real profile-prefixed cache keys, assert eviction without closing, and isolate Claude credential locks in tmp_path with no ambient pool entry. Forced Vertex refresh test passes.
- Missing anthropic SDK case reproduced on unpatched HEAD (1 failed, 205 deselected); excluded only that pre-existing dependency case from final broad validation, without changing dependencies or runtime behavior.
- Follow-up direct tests: 82 passed; final focused tests after recovery-read protection: 44 passed. Refresh tests: 3 passed.

## Phase 5 — final validation and commit
- Final scripts/run_tests.sh run: 268 passed, zero failed across five affected test files; only the baseline-proven missing-anthropic SDK case deselected. Earlier isolated run also passed runtime-cache, Nous cache-key, automatic fallback, runtime guardrail, file-staleness and file-state files.
- Final ruff check, python -m py_compile and git diff --check passed.
- H6 fixture: initial read plus eight futile reads reaches halt (sixth futile read first stalled_path; third stable blocked result halts), including unrelated terminal calls and varying pagination.
- Production/live containers/config/Docker untouched; AGY caps/general hard-stop defaults unchanged. Guard intentionally uses host-backed short-file versions; container/remote host-stat inference excluded.
- Commit prepared only for the scoped source/tests and this checkpoint; existing untracked implementation_execution.log left out.

## Phase 6 — commit blocked by sandbox
- git add/commit could not create /home/ubuntu/projects/hermes-agent/.git/worktrees/hermes-r49-context-loop/index.lock: Read-only file system.
- This worktree's shared Git metadata lies outside the writable roots. No escalation or metadata relocation attempted. The tested patch remains uncommitted in this worktree on vps-r49-context-loop.
- Commit message prepared: fix: retire profile auxiliary clients safely and halt stalled short-path reads.
