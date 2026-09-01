# Grid'5000 language jobs

These wrappers keep the Grid'5000 frontend lightweight and run GlotLID only
on a reserved node. They stage one shard at a time, use a 30-minute OAR job
with one GPU and two CPU cores, and give detection 25 minutes so the job can
stop with a durable checkpoint. The model itself is CPU-bound, so the GPU is a
resource-isolation and parallel-job requirement rather than an inference
acceleration claim.

The Seagate run and model cache remain canonical. The temporary Grid'5000
bundle is copied back before a completed shard or checkpoint is synchronized.
Never put credentials in the bundle, repository checkout, or shell
environment copied to a node.

## One resumable job

On the Mac, download the pinned public model into the Seagate cache and note
the resulting `model_v3.bin` path:

```bash
hf download cis-lmu/glotlid model_v3.bin \
  --revision 85cd671 \
  --cache-dir '/Volumes/Seagate M3/projects/osm-polygon-website-tag/models/glotlid'
```

Prepare a new bundle. The command refuses an existing bundle directory so a
finished or active bundle cannot be overwritten accidentally:

```bash
export OSM_POLY_RUN_DIR='/Volumes/Seagate M3/projects/osm-polygon-website-tag/runs/<run-id>'
export OSM_POLY_BUNDLE_DIR='/Volumes/Seagate M3/projects/osm-polygon-website-tag/grid5000/<bundle-id>'
export OSM_POLY_MODEL_PATH='/Volumes/Seagate M3/projects/osm-polygon-website-tag/models/glotlid/<snapshot>/model_v3.bin'
export OSM_POLY_COMMIT="$(git rev-parse HEAD)"
scripts/grid5000/prepare_language_detection.sh
```

Copy the repository checkout and bundle to a Grid'5000 frontend/job
directory with `rsync`. Do not run the detector on the frontend. From the
frontend, submit exactly one job:

```bash
export GRID5000_JOB_DIR='/home/<account>/osm-polygon-website-tag-job-<bundle-id>'
export GRID5000_REPO_DIR="$GRID5000_JOB_DIR/checkout"
export GRID5000_BUNDLE_DIR="$GRID5000_JOB_DIR/bundle"
scripts/grid5000/submit_language_detection.sh
oarstat -u
```

Before the first language job on a site, bootstrap the locked Linux runtime
once. It uses the same one-GPU, policy-aware submission boundary and stores
the environment/cache under the temporary Grid'5000 job directory:

```bash
export GRID5000_JOB_SCRIPT="$GRID5000_REPO_DIR/scripts/grid5000/bootstrap_language_runtime.sh"
scripts/grid5000/submit_language_detection.sh
```

After that job reaches a terminal state, clear its active marker and restore
the default job script before submitting a detection bundle:

```bash
rm "$GRID5000_JOB_DIR/job.active"
unset GRID5000_JOB_SCRIPT
```

The submit wrapper runs `usagepolicycheck -t` immediately before and after
`oarsub`, records the job ID in `job.active`, and refuses a second submission
while that marker exists. Inspect the job with `oarstat`; cancel only the
confirmed job with `oardel <job-id>` if necessary.

After the job finishes, copy the bundle back to the Seagate bundle directory,
then synchronize it:

```bash
export OSM_POLY_BUNDLE_DIR='/Volumes/Seagate M3/projects/osm-polygon-website-tag/grid5000/<bundle-id>'
scripts/grid5000/sync_language_detection.sh
```

A paused result changes no canonical shard bytes; it installs only the
verified `.language.parts` prefix. Prepare a fresh bundle from that Seagate
checkpoint and submit the next short job. A completed result atomically
installs the v1.4 shard and updates the ordinary run manifest. Keep Grid'5000
copies only until checksums, the result receipt, and synchronization have been
verified; remove only confirmed project-owned temporary files.
