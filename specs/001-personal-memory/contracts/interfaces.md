# Interface Contract v1

All times are UTC ISO-8601 strings. JSON objects use snake_case. Public DTOs reject invalid
enum values, empty/oversized text, invalid timestamps and negative budgets. Default target is cloud.
Private data is available to agent-facing tools only when target=local. The local admin list,
detail and graph HTTP endpoints default to local for the management UI. Search, recall and all
MCP responses default to cloud. HTTP errors are JSON detail with 400/404/409/422.

## Python package and DTOs

Package bmem, Python >=3.12. Models in src/bmem/models.py use Pydantic v2.

EventInput fields

```text
body str (1..100000)
harness str = "manual"
device_id str = "local"
native_event_id str (generated uuid when absent)
operation_id str (generated uuid when absent)
source_type user_declaration|user_dialogue|verification|agent_inference|external|injection_reference
  = user_dialogue
occurred_at datetime (now when absent)
session_id str = "manual"
project str = ""
sensitivity normal|private = normal
metadata dict = {}
```

DeclarationInput extends EventInput with source_type fixed user_declaration, kind
episode|fact|decision|method|preference = fact, fact_key str optional, memory_id str optional,
expected_revision int optional, conditions dict = {}, derived_from list[str] = [].
memory_id corrections require expected_revision. New facts may carry fact_key for deterministic matching.
RecallInput has query str = "", harness/session_id/turn_id/task/context_epoch strings,
budget int = 2000 (0..10000), target local|cloud = cloud, conditions dict = {}, project str = "".
Search filters use kind/status/harness optional, target local|cloud, conditions dict, project as rank hint.
DomainError(Exception) has status_code int, message str. Export now_iso() and redact(text).

## Store public interface

Store(home: Path) creates home/memory.db and home/evidence. Connections use WAL, foreign_keys,
busy_timeout. Short writes serialized with a lock, no model calls inside store. Every method returns
JSON-ready dict/list except paths explicitly noted. Store validates DTOs only at public boundaries.

```python
ingest(event: EventInput | dict) -> dict  # event_id, duplicate, job_id, optional memory
declare(data: DeclarationInput | dict) -> dict  # memory, event_id, duplicate
get_event(event_id: str) -> dict
get_memory(memory_id: str, target="local") -> dict  # includes revisions, sources, decisions
list_memories(query="", kind=None, status=None, harness=None, target="local",
              limit=100, offset=0, conditions=None, project="") -> list[dict]
search(query: str, limit=20, target="cloud", conditions=None, project="",
       semantic_hits=None) -> list[dict]  # each hit includes score, matched_by
recall(data: RecallInput | dict, semantic_hits=None) -> dict
    # items, text, token_count, injection_id, deduplicated, degraded optional
forget(memory_id: str) -> dict  # tombstone and recursive derived cleanup
stats() -> dict  # memories int, status_counts/kind_counts dict, events int,
    # jobs {pending,running,done,failed,total} counts, injections/imports int, semantic_enabled bool
records(name: str, limit=100) -> list[dict]  # allowlist arbitration,injections,import_cursors,jobs
graph(target="local") -> dict  # nodes, edges {source,target,relation}
get_settings() -> dict  # semantic_enabled default False
set_settings(values: dict) -> dict  # enabling queues embed jobs for current memories
record_import(source: str, values: dict) -> dict
backup(destination: Path | None = None) -> str  # tar.gz with online DB backup + evidence + tombstones
restore(archive: Path) -> dict  # validates archive, closes connections, merges local delete journal
rebuild_indexes() -> dict
```

Worker interface in Store

```python
claim_job() -> dict | None  # job_id,kind,event_id,payload,attempts; startup requeues abandoned running
finish_job(job_id: str) -> None
fail_job(job_id: str, error: str) -> None  # bounded backoff, sanitized error
apply_candidate(event_id: str, candidate: dict, model_id: str, *, recompute=False) -> dict
    # candidate body,kind,fact_key,conditions,confidence,derived_from, action,
    # optional memory_id,expected_revision; action ADD|UPDATE|NOOP|DISPUTE|QUARANTINE
    # source trust and sensitivity derived from event, candidate cannot elevate them
injected_context(harness: str, session_id: str, before: str | None = None) -> list[dict]
    # read-only exact delivered revisions, ordered by injection time, excluding erased bodies
preceding_user_event(harness: str, session_id: str, before: str) -> dict | None
    # read-only preceding user dialogue or declaration, bounded by occurrence time
promote_due(now: str | None = None) -> dict
maintain(now: str | None = None) -> dict  # daily consolidation jobs / weekly low-importance decay
```

Incoming injection markers have stable format `[bmem-injection:<injection_id>]`.
Persisted references never increase proof count. Recall text starts with a reference-data header and
marker, includes per item memory_id/revision/source/date/conditions. Whole-item budgeting includes
header and marker. Dedup key is harness/session/task/context_epoch/target/memory_id/revision.
Search filters effective status, conditions, sensitivity and revision after semantic hits merge.

Model-originated NOOP candidates bind body, kind, conditions, fact_key and expected_revision
from the same advertised target snapshot. The originating event retains the observation text.

## Runtime interface in src/bmem/runtime.py

```python
Runtime(home: Path)
status() -> dict  # available, version, models, install state/progress/error, local endpoint
install(embedding_only=False, update=False) -> dict  # blocking, called in background by API
embed(texts: list[str]) -> list[list[float]]  # bge-m3 only, 1024 finite floats, local only
distill(event: dict, existing: list[dict]) -> list[dict]  # pinned Qwen3 4B Instruct, bounded schema prompts
register_service() -> dict  # launchd/systemd, honor BMEM_CONFIG_ROOT, isolated tests supported
uninstall_runtime() -> dict
VectorIndex(home: Path)
upsert(memory: dict, vector: list[float]) -> None
search(vector: list[float], limit=20) -> list[dict]  # memory_id,revision_no,score
rebuild() -> None
Worker(store: Store, runtime: Runtime)
run_once() -> dict  # claims and handles one job, retries failures, promotes due proposals
search(query: str, limit=20, target="cloud", conditions=None, project="") -> list[dict]
recall(data: dict) -> dict  # semantic merge then Store.recall
```

Store declaration returns immediate memory. Worker distill job must avoid duplicate creation
when the event already produced a memory. Worker embed jobs skip inactive/deleted versions.
Worker.search and recall fall back to keyword results with degraded status on endpoint failure.
Ollama BMEM_OLLAMA_URL defaults http://127.0.0.1:11434. Native model endpoints are local-only.
Runtime lock metadata includes version and model digests, status persists real installation stages.

### Reconciliation and derived recomputation

Declaration jobs include immediate_memory_id and reconcile. Free-text declarations use reconcile=true.
Explicit fact_key or memory_id declarations use reconcile=false and can skip model calls.
get_event includes immediate_memory_id when its immediate declaration exists, including events
accepted through ingest. Worker excludes that immediate item from existing matching candidates.
The model may UPDATE/NOOP an older matching memory, and Store supersedes the immediate duplicate
after successful integration. Completely irrelevant content produces candidates=[], repeated support
produces NOOP with memory_id and the target's canonical body, so Store retains independent proof.

Sources retain invalidated_at metadata. Only valid independent roots count as evidence. Correction
invalidates the affected roots while preserving independent remaining sources. A derived item awaiting
recompute stops retrieval and promotion. Its distill job includes recompute=true, memory_id,
expected_revision and valid_event_ids. Worker loads exactly these remaining events, asks the local
model to regenerate this target, and calls apply_candidate(..., recompute=True) with the fixed target,
expected_revision, action UPDATE and valid source references. This trusted keyword is never accepted
from model JSON or a public HTTP/MCP payload. It bypasses ordinary declaration duplicate absorption.
No surviving evidence or no supported candidate leaves the derived item retracted. Successful CAS
creates a fresh revision and recomputes its evidence and state. Failed jobs retain retryable state.

## HTTP API

create_app(home: Path | None = None, start_worker=True) -> FastAPI in src/bmem/api.py.
BMEM_HOME default ~/.botinkit-mem. Server binds 127.0.0.1, port BMEM_PORT default8790.
API endpoints use shared DTOs and Store/Worker. No direct memory-text edit endpoint.

| Method | Path | Request / result |
|---|---|---|
| GET | /api/health | service, schema_version, worker |
| POST | /api/events | EventInput / ingest result |
| POST | /api/declarations | DeclarationInput / declaration result |
| GET | /api/memories | q,kind,status,harness,target,limit,offset / {items} |
| GET | /api/memories/{id} | target / memory detail |
| POST | /api/memories/{id}/forget | / forget result |
| GET | /api/search | q,target,limit / {items} |
| POST | /api/recall | RecallInput / recall result |
| GET | /api/stats | stats |
| GET | /api/arbitration | {items} |
| GET | /api/injections | {items} |
| GET | /api/imports | {items} |
| GET | /api/jobs | {items} |
| POST | /api/imports | {harness,paths?:[str]} / import result |
| GET | /api/graph | graph |
| GET | /api/sources | {items} adapter doctor |
| GET/PUT | /api/settings/semantic | / {enabled:bool} / settings incl semantic_enabled |
| GET | /api/runtime | runtime status |
| POST | /api/runtime/embedding/install | / {started:true}, background task, poll /api/runtime |
| POST | /api/backup | / {path} |
| POST | /api/restore | {path} / restored |
| POST | /api/worker/run | / run_once result |
| POST | /api/indexes/rebuild | / index result |
| POST/GET | /mcp | official SDK Streamable HTTP |
| GET | / | static UI |

MCP tools are memory_recall, memory_search, memory_expand, memory_save, memory_declare,
memory_forget and memory_status. memory_save is explicit T1 with source harness=mcp.
All expose sensitivity target controls default cloud. Save/declare use declaration DTO fields.
Private save/declare with cloud target return only id/status metadata, with no private body,
revisions or sources. trust_tier is always a string T1/T2/T3/T4. UI must render these directly.
Health service is the product identifier bmem, worker.running is a boolean. UI derives online state
from a successful health response. Jobs use kind and status, graph nodes use memory_id and body.

## Adapters in src/bmem/adapters.py

```python
connect(harness: str, config_root: Path | None = None, url: str | None = None) -> dict
doctor(config_root: Path | None = None) -> list[dict]
hook(harness: str, event: str, payload: dict, url: str | None = None) -> dict
import_history(store: Store, harness="all", paths: list[str] | None = None,
               config_root: Path | None = None) -> dict
```

Accept claude-code/codex/dsh. config_root defaults BMEM_CONFIG_ROOT then Path.home().
Connect preserves and backs up existing config, is idempotent. Discover current local versions and
formats from installed files/help before writing hooks/plugins. Claude JSON hooks and MCP, Codex
TOML MCP plus version-supported hook config, DSH native plugin/MCP. Unsupported hook versions
report partial. No invented adapter capability claims. Hook posts via httpx with short timeout and
returns additionalContext in native protocol. Captures user dialogue and transcript deltas with stable
event identity, assistant content stays T3, arbitrary tool text T4. Explicit remember/记住声明 T1.
History readers support Claude JSONL, Codex rollout JSONL, DSH JSONL/Zstd, Markdown memory files.
Prefer available Codex App Server history protocol when actually supported, report fallback otherwise.

## CLI

Entry points bmem=bmem.cli:main and bmemd=bmem.cli:daemon_main. Commands support --help.
Global --home and --url, BMEM_HOME/BMEM_PORT. JSON output for automation, errors stderr nonzero.
init [--skip-models] [--skip-service] creates DB then runtime provisioning and service registration.
start [--port] starts foreground loopback daemon. doctor/status/web/connect/hook/import/search/recall/
declare/forget/backup/restore/runtime install|status|update/uninstall/index rebuild.
declare supports --memory-id --expected-revision --fact-key --kind --sensitivity --conditions JSON.
hook reads stdin JSON, connect honors BMEM_CONFIG_ROOT. Service operations use API where running,
init/doctor/backup/restore/import support local Store when appropriate. No parallel direct writes
from hooks. Sensitive body values are never included in error logs.
