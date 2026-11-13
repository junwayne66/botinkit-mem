<!-- Sync impact report
Initial adoption 1.0.0 on 2026-11-12.
Added five principles, platform constraints and delivery workflow.
No template changes or deferred placeholders.
Amendment 1.1.0 on 2026-11-12 pins the explicit 4B Instruct distillation tag
after the generic tag resolved to a thinking-only model during functional testing.
-->
# botinkit-mem Constitution

## Core Principles

### I. Personal memory with explicit provenance

All supported Harness clients MUST share one personal memory authority. Project, device and
Harness labels MUST describe provenance and relevance. Every memory MUST retain source events,
conditions, occurrence time and version history. Default retrieval MUST search the entire library.

### II. Durable authority and rebuildable views

Events, revisions, source relations, arbitration decisions, injections, jobs and import cursors
MUST be durable. Event acceptance and its outbox job MUST commit together. Model calls MUST occur
outside write transactions. Search indexes and summaries MUST be rebuildable. Corrections MUST
use compare-and-swap versions and invalidate affected derived records. Privacy erasure MUST
remove content while preserving a content-free tombstone and prevent restoration from reviving it.

### III. Autonomous governance with bounded trust

Declarations MUST enter as evidence and produce audited decisions. T1 declarations and structured
T2 verification may become immediately active. T3 inference MUST observe the specified evidence
or probation thresholds. External T4 rules MUST remain isolated. Repeated references to one
source MUST count as one independent proof. Every lifecycle decision MUST record its reason,
evidence and model or policy version.

### IV. Local operation and controlled disclosure

Credential redaction MUST precede persistence, including evidence files. Private content MUST
only enter local model contexts. Recall packages MUST identify memories as reference data and
include provenance. The service MUST bind to loopback. Model failure MUST preserve capture and
keyword retrieval and retain retryable jobs.

### V. Small, verifiable delivery

Implementation MUST follow the current feature spec, plan and task list. Reuse existing design
assets and keep dependencies behind the concrete boundaries required by this release. Validate
external input at public entry points. Avoid speculative frameworks and redundant checks.
Functional acceptance MUST use real implementation entry points. Executor claims, independent
review and executed test results MUST be reported separately.

## Platform Constraints

The v0.1 authority is one local SQLite WAL database and one bmemd process, with a single background
worker. Managed local models are qwen3:4b-instruct-2507-q4_K_M and bge-m3. The embedding space is 1024-dimensional
bge-m3 with cosine distance. First install and repair use pinned runtime metadata. Upgrades are
explicit. The existing native JavaScript UI and design tokens are the frontend starting point.
The second-phase multi-computer design remains a subsequent feature.

## Development Workflow

Codex owns architecture, Spec Kit artifacts, task dispatch, integration and functional testing.
DSH executes bounded development tasks in parallel using existing third-party providers.
Claude performs independent code review using an existing third-party provider. Model selection
uses the least costly configured model adequate for each task, with escalation for observed
failures. Prompts reference only relevant project files. Credentials and private shared memory
MUST stay outside feature artifacts and external prompts. Global Harness configuration and
startup registration are exercised in isolated test homes during development.

## Governance

User instructions and project requirements govern amendments. Semantic changes update this
document with an impact report and semantic version. The architect checks compliance before
implementation and at convergence. Exceptions require a concrete requirement and recorded
rationale. Review findings become tasks in the same feature ledger.

**Version** 1.1.0 | **Ratified** 2026-11-12 | **Last Amended** 2026-11-12
