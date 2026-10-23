# EduAgent

[English](README.md) | [简体中文](README.zh-CN.md)

A local personal learning workspace built with React, FastAPI, PostgreSQL/pgvector and explicit AI operations. Organize your own materials by subject, review questions, practice, inspect grades and disputes, and ask an AI teacher with traceable references.

The interface is primarily in Chinese. Personal learning has no teacher/student business login; changing settings still requires local management authorization.

## Current status

The personal-learning feature was accepted with boundaries on **2026-10-23**. T001–T087 are checked, including user-approved conditional acceptance of T083/T084/T085/T087. **87/87 checked does not mean every test or real-model scenario passed.** See [known boundaries](#known-boundaries), the [specification](specs/002-personal-learning/spec.md) and [task record](specs/002-personal-learning/tasks.md).

The historical v2.0 tag describes the earlier teacher/student application. Its Gradio screens, role login and demo-account instructions are not the current entry point. The Python package version remains 0.1.0; feature acceptance and Git revisions are tracked separately.

## Capabilities

- Manage subjects, material versions and explicitly confirmed knowledge points.
- Import PDF, DOCX, TXT, Markdown, PNG and JPEG through the applicable material/paper paths, retaining originals, anchors, media and uncertainty markers.
- Correct extracted questions, answers and duplicate identities before admitting them to practice.
- Select uploaded, AI or mixed question sources and inspect actual coverage/shortages before starting.
- Submit a whole practice set. Objective and blank subjective answers use deterministic local rules; AI grading, regrading and answer supplementation require confirmation.
- Preserve grading attempts and disputes. Regrading does not silently overwrite history or resolve a dispute before acceptance.
- Inspect evidence-based diagnosis and material suggestions. Teacher references open the source; chat does not change official grades.

Provider quality depends on the model and input. Failed and unknown outcomes remain visible rather than being automatically resent.

## Fastest start: Docker

Install Git and Docker with Compose, and start the Docker engine. This route builds the frontend/backend inside Docker; host Python, Node.js and local model weights are not required just to open Setup.

Use a fresh checkout and a new Compose project/data volume. The following steps do not adopt an existing deployment.

From PowerShell:

~~~powershell
git clone --branch main https://github.com/MiracleYjx/EduAgent.git
cd EduAgent

# Choose other free local ports if necessary.
$env:POSTGRES_PORT = "25432"
$env:REDIS_PORT = "26379"
$env:BACKEND_PORT = "8000"

docker compose -p eduagent-local up -d --build
docker compose -p eduagent-local exec -T backend python -m backend.app.deployment.container_control --port 8000
~~~

Open the one-time authorization URL returned by the last command in your local browser. If BACKEND_PORT changes, pass that same published port to container_control. Downloads and the first build require network access; no fixed startup duration is promised.

Use these **container-side** addresses in the wizard:

| Setting | Docker application value |
| --- | --- |
| PostgreSQL host / port | postgres / 5432 |
| PostgreSQL database / user | eduagent / eduagent, unless overridden |
| Redis host / port / database | redis / 6379 / 0 |
| Managed business files | /app/storage/storage |

The 25432/26379 ports are for clients outside Docker, not the container's dependency settings. Default Compose uses local development trust authentication for PostgreSQL and loopback host publishing; this is not a public multi-user deployment template.

Preview and confirm initialization of the **new empty target**, then activate/restart as requested. Setup may be available while business readiness is 503. Opening a page does not initialize an old database or trigger AI operations.

### Docker AI credentials

Docker uses read-only mounted Secrets rather than Windows DPAPI. Keys, configuration ciphertext and model weights are not repository/image contents.

1. Create a private directory outside the checkout. Save your provider key in a UTF-8 file named provider-key using your editor, not a command-line argument.
2. Configure the absolute directory and nonsecret catalog below. Add separate files/catalog entries if chat and Embedding have different credentials.
3. Recreate the backend with the overlay, request a new authorization URL and select the mounted credential in Setup. Saving, probing and activating are separate steps; review any operation that sends a real request.

~~~powershell
# Replace this with your own existing private directory.
$env:EDUAGENT_SECRETS_DIRECTORY = Join-Path $env:LOCALAPPDATA "EduAgent/github-secrets"
$env:EDUAGENT_SECRET_MOUNTS = '{"provider-key":{"path":"/run/secrets/provider-key","purpose":"provider"}}'

docker compose -p eduagent-local -f docker-compose.yml -f docker-compose.secrets.yml up -d backend
docker compose -p eduagent-local exec -T backend python -m backend.app.deployment.container_control --port 8000
~~~

Secret files must satisfy the mounted-credential backend's ownership/access checks. Keep the same ports, project name and Compose-file selection when managing this deployment. Missing keys or weights produce an unready capability; they do not authorize automatic requests.

### Stop and resume

~~~powershell
docker compose -p eduagent-local stop
docker compose -p eduagent-local start
~~~

Named postgres_data, redis_data and storage_data volumes hold persistent state. Do not use down -v or volume prune for routine shutdown. Keep the original environment/overlay selection for a Secrets-enabled deployment.

## Windows source development

Requirements: Python 3.12 (the declared minimum is 3.12), Node.js 24 as used by the Docker build, Git, PostgreSQL/pgvector and Redis. Docker can run only the two dependencies.

From the fresh checkout in PowerShell:

~~~powershell
py -3.12 -m venv .venv
./.venv/Scripts/python.exe -m pip install -c packaging/requirements-windows.txt -e ".[dev,ocr]"

$env:EDUAGENT_PYTHON = (Resolve-Path ./.venv/Scripts/python.exe).Path
Push-Location frontend
npm ci
npm run build
Pop-Location

$env:POSTGRES_PORT = "25432"
$env:REDIS_PORT = "26379"
docker compose -p eduagent-local up -d postgres redis

$env:EDUAGENT_ENVIRONMENT = "development"
$env:EDUAGENT_DATA_ROOT = Join-Path $env:LOCALAPPDATA "EduAgent/github-local"
$env:EDUAGENT_SETTINGS_UI_ROOT = (Resolve-Path ./frontend/dist).Path
./.venv/Scripts/python.exe scripts/launch_windows.py
~~~

In this **host application's** wizard, use PostgreSQL at **127.0.0.1:25432**, Redis at **127.0.0.1:26379**, and the default database/user eduagent. Use your own unused data root and confirm the actual initialization target. The launcher handles local authorization and its worker shutdown.

The frontend build and explicit EDUAGENT_SETTINGS_UI_ROOT are necessary for the source launcher's React pages. GitHub does not supply frontend/dist. EDUAGENT_PYTHON ensures the frontend API check uses the intended Python environment.

The new entry point does not implicitly import a checkout .env or someone else's config.env. The .env.example file is a legacy compatibility reference; import old settings explicitly through Setup.

## Model choices

| Capability | Used for | Preparation |
| --- | --- | --- |
| Remote chat / JSON / vision | Selected AI generation, answer completion, subjective grading and teacher operations | Configure a supported provider/model and your own protected credential |
| Remote Embedding | Material indexing and configured semantic/hybrid retrieval | Configure a compatible Embedding service separately; a chat key alone is insufficient |
| Local BGE | Indexing/retrieval when a local provider is selected | Install .[rerank-local], obtain compatible weights, select their absolute resource directory |
| Local RapidOCR | Explicit OCR of retained scanned pages/images | Install .[ocr] and preinstall the three approved PP-OCRv5 mobile ONNX files |

A clone does not need the developer's model directories. Text/keyword paths do not universally need local weights. Current personal generation and teacher context use explicit keyword retrieval; grading can request Embedding for semantic/hybrid modes.

RapidOCR expects ch_PP-OCRv5_det_mobile.onnx, ch_PP-OCRv5_rec_mobile.onnx and ch_ppocr_mobile_v2.0_cls_mobile.onnx. The [adapter](backend/app/ai/ingestion/ocr/rapidocr.py) checks hashes and uses local ONNX CPU inference without silently downloading substitute weights.

For Docker, mount models read-only and use a **container-side** absolute resource path. The default image omits Torch/sentence-transformers, so local BGE also needs an explicitly prepared image. The default Windows package omits these libraries unless built with WithLocalModels. Changing Embedding identity requires the index validation/rebuild workflow, not just a path change.

## Build a Windows EXE

~~~powershell
./scripts/build_exe.ps1 -Python python -BuildRoot .cache/exe-build
~~~

Copy the **entire** generated EduAgent directory, including _internal. The package removes the target machine's Python/Node requirement; PostgreSQL/Redis, private configuration, API keys and optional weights remain external. This source repository does not imply that a GitHub Release asset is available. See [package notes](packaging/README.md).

## Validation

With the Python environment installed and EDUAGENT_PYTHON set:

~~~powershell
Push-Location frontend
npm test
npm run build
Pop-Location
~~~

Backend suites are under tests/unit, tests/contract and tests/integration. PG/Redis/Docker tests require identity-verified, owned Test profiles and dedicated resources; never point them at a personal database. Read [validation prerequisites](specs/002-personal-learning/quickstart.md) and [PG helpers](tests/postgres_helpers.py) before initializing test resources. Mock/build success does not establish provider quality or complete browser acceptance.

| Endpoint | Meaning |
| --- | --- |
| / | Personal learning UI |
| /setup and /settings | Setup and protected configuration |
| /health | Process liveness |
| /setup-ready | Setup/static-resource status |
| /ready | Business readiness; 503 does not mean Setup is unavailable |

## Known boundaries

- Four historical outcome_unknown operations remain in the developer's acceptance environment. Later independent samples succeeded but did not compensate or resolve those original requests. New installations do not import this history.
- T084/T085 journey records remain api_passed_browser_not_claimed. Screenshots/UI readbacks are partial evidence; complete browser coverage and the T083 real-AI browser-through-result path are not claimed passed.
- DOCX has limited historical T033 coverage. T084/T085 did not retest DOCX on both Windows and Docker.
- A real G1 operation completed but failed admission with ANSWER_ENCODING_INVALID; oracle_pass=false remains. Conditional acceptance does not turn it into a quality pass.
- The ledger schema does not persist finish_reason. Request-count disputes and historical authorization uncertainties remain documented.
- Checkbox counts do not guarantee accuracy, latency, clean-machine installation or supplier billing. Local .cache acceptance artifacts are intentionally not distributed with GitHub source.

The acceptance decision and future improvement entry points are in [tasks.md](specs/002-personal-learning/tasks.md), [spec.md](specs/002-personal-learning/spec.md) and [quickstart.md](specs/002-personal-learning/quickstart.md).

## Repository map

~~~text
backend/       FastAPI, models, parsing, retrieval, AI and local management
frontend/      React UI, generated API types and frontend checks
migrations/    Alembic schema history
packaging/     Windows build recipe and tested runtime constraints
scripts/       Launch, build, benchmark and explicit maintenance
tests/         Unit, contract, isolated integration and synthetic fixtures
specs/         Versioned requirements, contracts and acceptance boundaries
benchmark/     Historical reproducible benchmark inputs
~~~

Runtime data, .env files, credentials, weights, logs, builds and .cache evidence are not installation inputs. Supply your own lawful materials and model access. Automatic textbook collection, image-answer grading and a separate scoring workflow inside chat are outside the first-version scope.
