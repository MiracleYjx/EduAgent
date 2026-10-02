# T160 portable correction UI evidence tools

These tools replay the actual first-round import snapshots solely to measure the real committed correction panel. They do not perform OCR, extraction, image understanding, or paid model calls. The source snapshot and isolated database/files must already exist; these scripts do not switch the user checkout or migrate the business database.

The successful running cache version is preserved byte-for-byte in `frozen-runtime-sources/actual-ui-sources.zip`, with an archive-member digest receipt. ZIP avoids Git line ending conversion of historical source bytes. `sources-receipt.json` records original and portable SHA256 values for traceability. Formatting/path parameterization changes are separate from the running measurement version; no hash equality gate is added.

## Explicit paths

The portable launcher requires `--repo-root`, `--batch-root`, and `--source-root`. The batch root contains the existing `isolation.json` and `business-files/`; source root is the committed `source-head` export. The launcher privately reads repository .env values, then overrides DATABASE_URL/REDIS_URL/STORAGE_ROOT/PYTHONPATH for the owned isolated resources. Never archive .env, JWTs, passwords, or connection strings. VISION_MODEL is empty for this UI worker.

Prepare fresh replay instances if needed:

```text
python correction_ui_launch.py --repo-root <repo> --batch-root <batch> --source-root <source-head> --prepare-replays --quality-pointer <actual-quality-output-pointer.json> --quality-output <actual-quality-directory> --fixtures <new-fixtures.json> --output <fresh-prepare-log-directory>
```

The optional quality-output override supports relocated actual evidence. Preparation consumes automatic_snapshot from the actual r1 IMP-TEXT, IMP-IMAGE, and IMP-CROSS result.json files, verifies each legitimate Pending Review result, reads registered actual originals, and creates 27 new identities. It is labeled source_backed_replay_for_ui_timing and is not a new extraction. Do not prepare during independently measured import-performance work.

Start a cold worker:

```text
python correction_ui_launch.py --repo-root <repo> --batch-root <batch> --source-root <source-head> --fixtures <fixtures.json> --workload text --temperature cold --repeat 1 --port 7871 --output <fresh-text-cold-1-directory>
```

Each workload needs separate cold repeats 1/2/3. Stop only the owned service PID after validating its command line, then start the next fresh output directory. Do not clear OS caches or use prior completion records as new runs.

For warmup plus five warm runs, start one worker with --temperature warm. The initial acceptance instance is warmup; use the wrapper's acceptance-instance dropdown to load fresh warm-1 through warm-5 in the same worker. Wait for each initial page and fields before the measured native target choice.

## Actual action and duration

Text: original one-page PDF, question 2 to question 1. Figure: original image paper, question 1 to question 2. These are question switches, because the originals have one page. Cross: the same question 4, original page 1 to page 2; existing structured fields stay unchanged. Production question selection calls reload for all fields and the default first source page; production page selection updates page image/facts only.

Only root controls the browser through Cua. No Playwright, Selenium, CDP, or substitute browser control is used. The observer reads actual DOM, images, native selection events, and browser performance.now. Capture uses the option's aria-label (hidden check marks are not part of its label), permits portaled options, requires the designated dropdown to be expanded, and records the actual pointerdown/click/Enter/input start event. Opening a menu alone is not a measurement.

The end state requires actual target fields and real page alt/size, all images loaded, no visible queue busy, interactive fields, and stable double requestAnimationFrame. DOM stability alone does not prove the target was loaded. Fifteen-second timeout and original errors remain recorded. Cua transport time is not used as elapsed_ms; <500ms remains the target and a successful render can still miss it.

The installed marker is document.documentElement.dataset.t160Observer='installed'. Current Gradio frontend inserts custom js directly into a script; the observer is therefore passed as an actual IIFE, not an uncalled arrow-function wrapper.

## Receipts and actual screenshots

Browser measurements are POSTed to /t160-observer and stored as start/end JSONL. GET /t160-observer-receipts exposes only already-recorded observations. Fields contain synthetic actual question data and no credentials. Screenshots must come from actual Cua getScreenshot bytes. POST PNG/JPEG bytes to /t160-screenshot/{run_id}; the service saves the received file and a separate receipt. Planned screenshot references do not mean a file exists.

The preflight directories and setup diagnostics are retained. A setup with no actual browser start is excluded from formal measurement counts. A measured timeout/failure is an attempt and cannot be silently replaced. Duplicate measured run IDs are reported as ambiguous rather than selecting the fastest result.

## Aggregate after all browser work finishes

```text
python aggregate_ui_results.py --input <ui-correction-group-directories> --fixtures <actual-27-fixture-manifest.json> --output <fresh-summary-directory>
```

The aggregator reads files only; it has no browser, database, or model access. It writes a fresh summary directory with summary.json, attempts.json, timings.csv, and preflight-exclusions.json. There are 24 timed actions and 3 separately listed warmups. Missing/duplicate IDs, absent ends, invalid durations, missing captures, failures, and over-target successes are explicit. Groups report min/median/max of actual successful durations without dropping slow samples.

For resource evidence, only samples whose full sample cycle falls within the actual browser action UTC window and whose PID matches service_pid contribute. Short actions may have zero complete one-second samples; then every action-window peak is null. The full worker lifetime peak is not substituted. A sparse partial observation does not prove the resource budget; the shared PostgreSQL container scope and repeated shared RSS pages remain explicit.

The preparation scripts and observer received syntax/static lint checks. The portable copy has not been used to replace or reclassify frozen measurements. Use the existing running receipts for actual results.
