import { readdir } from "node:fs/promises";
import path from "node:path";
import { DefaultArtifactClient } from "@actions/artifact";

async function filesUnder(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const candidate = path.join(directory, entry.name);
    if (entry.isSymbolicLink()) {
      throw new Error(`checkpoint contains a symbolic link: ${candidate}`);
    }
    if (entry.isDirectory()) {
      files.push(...(await filesUnder(candidate)));
    } else if (entry.isFile()) {
      files.push(candidate);
    }
  }
  return files.sort();
}

const [artifactName, snapshotDirectory] = process.argv.slice(2);
if (!artifactName || !snapshotDirectory || process.argv.length !== 4) {
  throw new Error("usage: node upload.mjs ARTIFACT_NAME SNAPSHOT_DIRECTORY");
}

const files = await filesUnder(snapshotDirectory);
if (files.length === 0) {
  throw new Error("checkpoint snapshot is empty");
}

const artifact = new DefaultArtifactClient();
const result = await artifact.uploadArtifact(
  artifactName,
  files,
  path.resolve(snapshotDirectory),
  { retentionDays: 3, compressionLevel: 6 },
);
process.stdout.write(`Published ${artifactName} (${result.size} bytes).\n`);
