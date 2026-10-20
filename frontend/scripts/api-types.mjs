import { execFileSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import openapiTS, { astToString, COMMENT_HEADER } from "openapi-typescript";

const root = fileURLToPath(new URL("../../", import.meta.url));
const target = new URL("../src/api/generated.ts", import.meta.url);
// Set EDUAGENT_PYTHON to the project's isolated test interpreter in CI/local use.
const python = process.env.EDUAGENT_PYTHON || "python";
const schema = JSON.parse(execFileSync(python,
  ["-I", "-B", "-X", "utf8", "scripts/export_settings_openapi.py"],
  { cwd: root, encoding: "utf8", maxBuffer: 8 * 1024 * 1024 },
));
const generated = COMMENT_HEADER + astToString(await openapiTS(schema));
if (process.argv.includes("--check")) {
  if (readFileSync(target, "utf8").replaceAll("\r\n", "\n") !== generated) {
    console.error("Settings API types are stale. Run npm run generate:api, then review consumers.");
    process.exitCode = 1;
  } else console.log("Settings API types match backend OpenAPI.");
} else {
  writeFileSync(target, generated, "utf8");
  console.log("Generated settings API types from backend OpenAPI.");
}
