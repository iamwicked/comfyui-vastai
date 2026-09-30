# Agent handoff: comfyui-vastai, Studio spec in progress

Written 2026-09-30 at the end of a working session. Read all of it before acting. The user-facing overview is [PROJECT_SUMMARY.md](PROJECT_SUMMARY.md).

## 1. Situation

The user (Aadi) maintains comfyui-vastai: idempotent scripts that turn a stock Vast.ai ComfyUI instance into a reproducible workstation. This session specified **Studio**, a web front end for that ComfyUI, spec-driven: requirements, then design, then tasks, each reviewed by the user before the next. Requirements are done and reviewed. The design is mostly written (three sections missing). There is no task list and no code yet.

Separately, the **ai-influencer-pipeline** spec is being implemented in other sessions in the same working tree. Studio reuses parts of that code.

## 2. Rules the user set (do not break)

- **Ask before changing any existing install command.** That covers `scripts/stock-setup.sh`, `scripts/common.sh`, `scripts/vast-provision.sh`, `scripts/provision.sh`, `scripts/install-workflow.sh`, `config/nodes.txt`, `config/catalog.txt`, `config/models.list`, `tests/requirements-check.txt` and the PORTAL_CONFIG guidance. Studio's proposals P-1 to P-7 (Requirement 23) are pending; none is approved. Put none of them in the task list until the user approves it.
- Tokens only in environment variables; never write, log or echo them.
- Shell scripts: idempotent, visible progress, `set -euo pipefail`, every expansion quoted, bash 3.2 compatible (the Mac's `/bin/bash` runs the checks), ShellCheck clean at default severity.
- Every change documented in `REFERENCE.md` and `CHANGELOG.md`.
- Studio talks to ComfyUI only through its API on 127.0.0.1:18188 (/object_info, /upload/image, /prompt, /ws, /history, /view, /interrupt, /queue, plus the image's /workflow/convert). It never touches ComfyUI's files or `/venv/main`, binds only to 127.0.0.1, and opens no unauthenticated port.
- Studio is FastAPI plus static HTML, CSS and JavaScript with no build step; every control labelled and keyboard operable. No workflow-specific code: a new workflow is a new manifest.
- Reuse the influencer Template_Filler (`scripts/influencer/templates.py`) and ComfyUI client (`comfy.py`, `http.py`) through additions only; every existing test keeps passing unchanged.
- Tests belong to the `scripts/check.sh` suite and run offline with the fake ComfyUI (`tests/fakes/comfy.py`).
- Git: do not commit, push or change git config unless asked.
- The user reviews each spec document before the next one. Keep replies short and plain.

## 3. The original request, condensed

Studio lets the user pick a workflow and a model, upload input images, add LoRAs, adjust settings, press Generate, watch progress and get the images or videos back.

- Use the UI-format graphs the repo installs in `/workspace/comfyui-user/default/workflows`, converted with the stock image's POST /workflow/convert (SethRobinson's converter) and cached.
- One small committed manifest per workflow maps UI controls to node inputs (by node title or class, node id as fallback): prompt, negative prompt, seed, steps, cfg, sampler, scheduler, size, batch, image inputs, video length and fps, and the model loader(s) LoRAs attach to.
- The form: workflow picker (installed workflows with their catalog description); model picker from /object_info; image slots with drag and drop and previews (qwen21-edit up to 10, krea-style and edit-2509 up to 3, wan-i2v 1); a LoRA stack (add, remove, reorder, strength -2 to 2, searchable incl. subfolders) applied as a chain of LoraLoaderModelOnly after the manifest's loader(s); the graph's own LoRAs listed and switchable; settings prefilled from the graph with per-workflow limits (MiniMax H3: 24 fps, 17n+5 frames, multiples of 32); seed with randomize; Generate, Cancel, live progress and previews; plain errors naming the missing node or model; gallery with downloads; history loadable into the form; presets per workflow; history and presets on /workspace.
- First version: krea2 (both graphs), qwen21, qwen21-edit, krea-style, edit-2509, wan-i2v. minimax-h3 and wan-animate later.
- Running: supervisor program on 127.0.0.1 in its own venv on /workspace, reached through Vast's Portal (PORTAL_CONFIG) or an SSH tunnel; one idempotent install command. The user suggested `bash scripts/stock-setup.sh studio` with `finish` restarting it, but asked to be asked first (hence P-1, P-2).
- Done when:
  - With krea2 installed: pick the official Krea 2 Turbo graph, add a LoRA at 0.8, fix the seed, Generate, and the image appears in the gallery; the same settings reproduce it.
  - qwen21-edit runs with three uploaded images.
  - Cancel stops a running job; a missing model is reported by file name.
  - Tests in the `scripts/check.sh` suite cover manifest loading, LoRA chain rewiring and job building against saved API-format fixtures, with a fake ComfyUI for API calls.

## 4. Repo and git state

- Workspace `/Users/aadiagrawal/Desktop/comfyui-vastai` (macOS, zsh). Branch `v6`, HEAD `a746fda` (v6).
- Uncommitted: edits to tracked docs, config lists and scripts, and untracked `.kiro/`, `config/influencer/`, `config/patches/krea2_simple.json.patch.json`, `scripts/check.sh`, `scripts/influencer.sh`, `scripts/influencer/`, `tests/`, `docs/`. This is mostly the v7 influencer work from other sessions.
- Influencer spec (`.kiro/specs/ai-influencer-pipeline/`): tasks 1 to 27 done, task 28 (Phase 1 checkpoint) next, Phases 2 to 4 not started. Do not change influencer code for Studio beyond the additive client and fake-server changes the Studio design names, and re-read those files first: other sessions may have changed them.

## 5. Studio spec files (`.kiro/specs/studio/`)

- `.config.kiro`: `{"specId": "0bff3105-7d29-4ce6-906e-df03894f1553", "workflowType": "requirements-first", "specType": "feature"}`. Do not recreate it.
- `requirements.md`: complete, reviewed, format-valid. Introduction, Glossary, the requirements in EARS form, then "Open items" (facts to confirm on an instance) and "Decisions for review" (defaults assumed everywhere). Criteria are cited across requirements by number ("Requirement 13 criterion 6"), so never renumber: refine in place and append. Use Glossary terms exactly (Generate request, Stored_Input, Consumer_Input, Target_Output, Install_Name, Providing_Id, Control_Limits, Valid_Form_State, Node_Label, Live_State, Live_Socket, History_Version, Studio_Core, and the rest).
- `design.md`: present, in order: Overview (with "What the repo shows" and the key design decisions), Architecture, Components and Interfaces (C1 to C25), Data Models (M1 to M8), Algorithms (A1 to A9), First-version Manifests, Fixture capture, Correctness Properties (numbered, for Hypothesis), Error Handling, Security, Service and Installer, Accessibility. **Missing: Testing Strategy, Documentation, Approval-gated changes.** The pass that would have written them was stopped by the user before it wrote anything.
- `tasks.md`: not created.

## 6. Next steps

### 6.1 Finish `design.md`

Append at the end, in this order, changing nothing above (except a cross-reference that would otherwise break). Read only the parts you need: the headings, the Module layout table, Fixture capture, the Correctness Properties headings, and the "Test hooks" part of Service and Installer.

1. `## Testing Strategy`
   - Files: `tests/studio/test_studio_*.py` (names unique across `tests/`), strategies in `tests/strategies_studio.py`, fixtures in `tests/fixtures/studio/`, and `tests/studio/test_studio_meta.py`, which checks the `# Feature: studio, Property N: <title>` tags against the design's `### Property N:` headings. The existing `tests/test_meta.py` reads only the influencer's tags.
   - Fake_ComfyUI additions (Req 26.6): GET and POST /workflow/convert answered from fixtures, GET /view with byte ranges, POST /queue with `delete`, and a /ws endpoint replaying scripted sequences with binary preview frames. `tests/fakes/_server.py` gains an optional header field on `Response` and an upgrade hook. All additive; the existing endpoints, script API and tests stay unchanged.
   - Stub PATH tests of `scripts/studio.sh` with `tests/helpers/stubs.py` and the design's test hooks (`SUPERVISOR_CONF_DIR`, `PORTAL_LOG_DIR`, `PORTAL_YAML`, `MAIN_VENV_DIR`, `STUDIO_PYTHON_CANDIDATES`, `WORKSPACE`).
   - Static Frontend tests: labels, no inline script, no external URL, one live region, and the rest of the Accessibility section's static table.
   - `.check-venv` holds pytest, Hypothesis, PyYAML, Pillow and NumPy, but no FastAPI. No test module may import `studio.web` or FastAPI at import time, or pytest collection fails. The Studio_Core is tested without FastAPI; the HTTP wiring is checked only on an instance unless P-6 is approved (Req 27.14).
   - Verified: `scripts/check.sh` needs no change. Its tests step runs `PYTHONPATH=<repo>/scripts HYPOTHESIS_PROFILE=check .check-venv/bin/python -m pytest -q tests`, so `tests/studio/` is collected and `import studio` works. Its `bash -n` and ShellCheck steps take every `*.sh` (or shell-shebang) file that `git ls-files --cached --others --exclude-standard` lists, so `scripts/studio.sh` is included once it exists.
   - A traceability table from each requirement to its properties, example tests or instance checks.
2. `## Documentation` (Req 25): the REFERENCE.md Studio section (install, update, restart, status; the tunnel command and other local ports; the Studio_Home layout and what a volume keeps; the manifest format and how to add a workflow; the converter requirement and fix; the next step per Plain_Error kind; limits and defaults; changing the port and Studio_Home; the warnings of Req 25.3; the Req 27 checklist), the §1 layout rows and instance paths, a README paragraph, and a CHANGELOG entry. Numbering: the influencer work already added REFERENCE.md §13 and a CHANGELOG v7 entry, and `tests/phase1/test_docs.py` checks that v7 entry. Studio takes the next free section and version when it lands and must not break that test; flag the conflict to the user.
3. `## Approval-gated changes`: open by saying none of these is in the default design or task list, and each is added only after the user approves it. Then P-1 to P-7, each with the exact file and lines, failure handling and tests. P-7 must say:
   - a global `config/nodes.txt` line changes the Selection of `global` and of a full run, which `tests/phase1/test_provision_bash.py::test_selection_matches_the_selection_snapshot` (against `tests/fixtures/selection-v6.txt`) refuses, and Req 5.6 keeps that test unchanged; the alternatives are a new opt-in catalog id, or amending the snapshot in both specs
   - the ref is a branch or tag, not a commit (the provisioner runs `git clone --depth 1 --branch <ref>`)
   - an existing `custom_nodes` folder of the same name (Vast's copy) is kept and fast-forwarded

Then validate the design's format, and check that its forward references hold: C17 points to Security, C25 to Service and Installer, and the Correctness Properties to the Testing Strategy.

### 6.2 Generate `tasks.md`

- Default path only: no task for P-1 to P-7. Say that approved proposals get their own tasks later.
- Tests the user named in "Done when" are required, not optional: manifest loading, LoRA chain rewiring, and job building against saved API-format fixtures, with the fake ComfyUI for API calls.
- Mark instance-only work clearly as needing the user on an instance: fixture capture, the manifests that depend on the fixtures, and the acceptance checks.
- Suggested order, following the design's Module layout:
  1. ComfyUI_Client additions in `scripts/influencer/comfy.py` and `http.py`: new methods only, including a stdlib WebSocket client and a streamed `/view`. Add the Fake_ComfyUI additions with tests, leaving existing tests untouched.
  2. Studio_Core pure modules with their property tests on synthetic strategies: `jsonio`, `catalog`, `manifest`, `objectinfo`, `graphs`, `conversion`, `readiness`, `forms`, `builder`, `plain`.
  3. `storage`, `images`, `jobs`, `comfyws`, `live`, `httprules`, `service`.
  4. The FastAPI layer (`scripts/studio/web/`) and the static Frontend (`scripts/studio/static/`).
  5. `config/studio/requirements.txt` (pinned, hashed) and `scripts/studio.sh` with stub PATH tests.
  6. The capture tool `tests/tools/capture_studio_fixtures.py`.
  7. On an instance: capture the fixtures, then write the seven manifests in `config/studio/manifests/` and the fixture-based tests (fixture property, manifest/fixture pairing).
  8. Docs: REFERENCE, README, CHANGELOG.
  9. Checkpoints that run `bash scripts/check.sh`, and a final instance acceptance checklist.
- Validate the task list's format.

### 6.3 Hand back to the user

Show the finished design and the task list. Ask which of P-1 to P-7 the user approves and for answers to the other "Decisions for review". Do not start implementing until the user says so.

## 7. Facts already established (do not rediscover)

Check suite and tests:
- `scripts/check.sh` runs four steps in order, each even after a failure: config (`influencer check-config`), tests, `bash -n`, ShellCheck. It is bash 3.2 compatible and runs on the Mac. `.check-venv` is built from the hashed `tests/requirements-check.txt`.
- The Hypothesis `check` profile: 200 examples, no deadline, derandomized. `tests/conftest.py` has a socket guard that allows loopback only.

Influencer code Studio reuses:
- `templates.fill`: the loaders of the i-th lora_list (name order, from 0) are nodes `str(900 + 20*i + k)`. `$prev` is the declared source for loader 1 and `[previous id, 0]` after that.
- `templates.check_values`: reads only a Template's name, placeholders and used set, and compares every int or float value with numeric min and max (pass plus or minus infinity where there is no bound).
- `Template` and `Placeholder` are public frozen dataclasses. `parse_template` cannot build a node-less Template.
- The Filler's own seed bounds are 0 to 4294967295 and must not change.
- `comfy.py`:
  - `submit` drops a 200 answer's `node_errors`, and `queue()` drops queue numbers.
  - `parse_history` keeps only `images`/`gifs` and the exception message.
  - `upload` defaults to the subfolder `influencer`.
  - `is_prompt_id`, `expand_autogrow` and `answer_excerpt` exist.
- `http.py`: `LoopbackClient.request` reads whole answers up to `MAX_LOOPBACK_BYTES` (64 MiB) and sends no Range header. There is no WebSocket code; `open_socket` gives a loopback-checked socket.
- `timing.py`: 10 s for /system_stats, /queue and /history; 30 s for /object_info and POST /prompt; 60 s for uploads; 2 s poll; 30 s interrupt grace. There is no Converter field, so Studio passes its own limits.
- `ids.py` imports `calendar_io`, which imports `yamlio` and so PyYAML. Studio must not import it. It writes canonical JSON itself: `json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`.
- `atomic.write_atomic` (temp file, fsync, rename, folder fsync) and `locks.ProcessLock` create files as 0666 minus the umask, so Studio sets umask 077.
- `influencer.lines` parses `catalog.txt` and `models.list` the way the bash does. Importing `influencer` runs a Python 3.10+ guard.

Instance and ComfyUI:
- Vast's ComfyUI image clones `SethRobinson/comfyui-workflow-to-api-converter-endpoint`, unpinned.
  - POST /workflow/convert takes the UI JSON as its body and answers the API graph with `_meta.title` on each node.
  - Bodies over 1 MB get 413. Errors come back as 400 or 500 with an `error` text.
  - GET /workflow/convert answers the name and version (2.3.0 when checked).
  - Nodes inside subgraphs get ids `<outer>:<inner>`.
- The Comfy-Org template graphs are built around subgraphs. qwen21-edit takes its up to 10 images through autogrow inputs.
- `/ws`:
  - `progress_state` reaches only the submitting client_id, so Studio keeps one client_id across restarts.
  - Binary frames start with a 4-byte type: 1 is a preview (then a 4-byte image type), 3 is text, and 4 is a preview with JSON metadata, sent only to clients that announce `supports_preview_metadata`.
  - Previews need `--preview-method` or `extra_data.preview_method`.
- supervisor's `environment=` only adds to supervisord's own environment, which holds HF_TOKEN and other credentials. `studio.conf` therefore starts Studio with `command=/usr/bin/env -i ...` so it gets a clean environment.
- Portal: PORTAL_CONFIG entries are `hostname:external_port:internal_port:path:name`, and the external port must be mapped when the instance is created. The layout of `/etc/portal.yaml` is unverified.

## 8. To confirm on an instance

These come from requirements.md "Open items" and the design:

- **Converter:** it is present on the rented image tag and on reused volumes, and its output matches ComfyUI's own API export for each graph. It keeps a graph switch such as "Enable Lightning LoRA" as one boolean input.
- **Portal:**
  - the `/etc/portal.yaml` layout, and whether changes need a Caddy restart or a reboot
  - the Host header Studio sees through the Portal and its tunnels
  - whether a Portal tunnel to Studio's own port skips the token check
  - whether same-origin `ws:` passes `connect-src 'self'` in each browser
- **ComfyUI API:**
  - support for `extra_data.preview_method` and `/ws` previews
  - whether POST /interrupt honours a prompt_id on this release
  - the `COMBO` + `options` form of /object_info
  - whether new model files are listed without a restart
  - whether `execution_success` arrives before the history entry
  - GET /view byte ranges
  - the seed bounds of non-KSampler seed inputs
- **wan-i2v:** the graph's file name, structure and two model loaders, and whether it follows a 4n+1 frame rule.
- Whether the same seed gives identical pixels. Req 27's reproduction check depends on it.
- **Instance environment:**
  - ports 18190 and 8190 are free
  - the system Python version (Studio's venv needs CPython 3.10 to 3.14 outside `/venv/main`)
  - Vast's `PUBLIC_IPADDR` and `VAST_TCP_PORT_22` variables, used for the tunnel command

## 9. Lessons from this session

- Long single generation steps hit capacity limits or ran out of time. Small passes worked: one to three sections each, reading only the parts they need.
- Requirements cross-reference criteria by number, so never renumber. Refine in place and append.
- The user wants to see and approve each document. Do not skip ahead to implementation or to changes in existing commands.
- Other sessions keep changing the influencer code. Re-read a file before relying on line-level details.
