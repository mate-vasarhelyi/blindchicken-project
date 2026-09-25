# Blind Chicken OpenProject deployment

This directory contains the fork's image, TrueNAS deployment, and upstream update workflow. The application image is built from `docker/prod/Dockerfile` with its `all-in-one` target. It includes PostgreSQL, background workers, and Hocuspocus collaborative editing. The TrueNAS app publishes only the web port.

## Build and release

`.github/workflows/bcp-image.yml` builds `linux/amd64` and pushes to GHCR on pushes to `main`. A manual dispatch also builds the selected branch, including a release or review branch. Each run tags the image `sha-<commit>`, then publishes the immutable `tag@sha256:digest` reference in the job summary and the `image-reference` artifact.

Start a candidate build from a review branch with `gh workflow run bcp-image.yml --repo mate-vasarhelyi/blindchicken-project --ref <branch>`. Feature branch runs are for build checks. The deploy helper accepts only successful runs from `main`. Before deploying, set the GHCR package visibility to public so TrueNAS can pull without registry credentials. The helper checks the digest through an anonymous registry request.

Run an upstream update from a clean local `main` branch:

```sh
./deploy/bcp/upstream-release.sh vX.Y.Z
```

The helper requires a published stable release on the official `opf/openproject` repository. It compares the release tag from GitHub's API with the commit fetched from the official repository, creates `upstream-vX.Y.Z` from the fork's `origin/main`, merges the tag commit, pushes the branch, and opens a PR to `main`.

If the merge conflicts, resolve the files on `upstream-vX.Y.Z`, run the focused checks below, then commit and push the merge. Open the PR with `gh pr create --base main`. Review the upstream release notes and diff before merging. Do not deploy a moving branch.

## TrueNAS setup

Create a ZFS dataset whose mountpoint will be `DATA_ROOT`, and include it in a periodic snapshot task. The deploy helper requires `DATA_ROOT` to be the dataset mountpoint and requires these directories to exist:

- `$DATA_ROOT/postgres`
- `$DATA_ROOT/assets`

The first holds PostgreSQL 17 data. The second holds uploaded files and other OpenProject assets. Both paths live under the same snapshot root. Create the dataset and directories in TrueNAS before running `create`. The helper checks the mountpoint and paths but does not create or configure them.

Set a DNS hostname and create a protected environment file on the devbox:

```sh
install -d -m 700 ~/.config
cp deploy/bcp/env.example ~/.config/bcp-openproject.env
chmod 600 ~/.config/bcp-openproject.env
$EDITOR ~/.config/bcp-openproject.env
```

The existing Cloudflare and NPM route uses `project.blindchicken.productions` and NAS port 8096. Set `DATA_ROOT` to the new dataset mountpoint, `SECRET_KEY_BASE` to a fresh hexadecimal secret with at least 64 characters, and `OPENPROJECT_SEED__ADMIN__USER__PASSWORD` to a fresh alphanumeric password with at least 32 characters. These values must be present before the public route can reach the app. Keep the env file outside Git. Values must be unquoted single lines. Leave the SMTP entries commented out unless SMTP is configured.

The compose config uses one digest-pinned image, keeps the database and attachments on the dataset, defaults to host port 8096, and does not publish PostgreSQL's port. It sets `OPENPROJECT_HTTPS=true` and the configured hostname so OpenProject builds secure URLs and uses WSS for Hocuspocus. NPM terminates TLS.

## Stage and deploy

Run these commands from the devbox with Docker Compose v2, GitHub CLI access to the fork, and SSH configured for `blindchicken-nas` with batch mode and non-interactive sudo:

```sh
python3 deploy/bcp/deploy.py stage --run <successful-main-run-id>
```

You can pass the digest reference from the job summary instead:

```sh
python3 deploy/bcp/deploy.py stage --image 'ghcr.io/mate-vasarhelyi/blindchicken-project:sha-<commit>@sha256:<digest>'
```

Staging validates the successful run and its artifact, checks that the image is publicly pullable, reads the protected env file, and runs `docker compose config`. It writes the fully rendered YAML with mode 0600 under `~/.local/state/bcp-openproject/stages/`. That file contains `SECRET_KEY_BASE` and any SMTP password. The helper prints its path and SHA-256, never its contents. Review the staged file locally before proceeding.

For the first install, create the TrueNAS Custom App explicitly:

```sh
python3 deploy/bcp/deploy.py create --stage <stage-id> --confirm-create
```

The helper refuses to create if `openproject` already exists, local deployment state exists, the dataset paths are missing, or the configured port is occupied.

For an update, create a TrueNAS snapshot of the dataset after reviewing the staged YAML. Then acknowledge that snapshot and the exact staged YAML hash. The helper checks that the named snapshot exists on the configured dataset:

```sh
python3 deploy/bcp/deploy.py acknowledge-backup \
  --stage <stage-id> \
  --review-sha256 <printed-yaml-sha256> \
  --snapshot 'pool/apps/openproject@before-update'
python3 deploy/bcp/deploy.py apply --stage <stage-id> --confirm-apply
```

The helper requires a running Custom App, a successful main image, the exact reviewed backup marker, and a match between TrueNAS's running image and the previous local deployment record. It saves the previous rendered YAML and image under the new stage's `rollback/` directory before applying. It sends only the rendered Compose config to the TrueNAS middleware over SSH; it does not copy the repository or build context to the NAS.

The helper uses `sudo -n midclt call` for TrueNAS middleware operations and sends the request through SSH stdin. The checked TrueNAS 25.10.4 host has sudo subcommand logging enabled, so create and update calls may record the rendered Compose argument, including `SECRET_KEY_BASE` and any SMTP password, in the sudo audit log. The helper does not print the rendered config. Restrict access to the TrueNAS sudo logs.

To roll back, stop the app, restore the pre-update dataset snapshot in TrueNAS, then reapply the saved previous render and image through the TrueNAS Custom App editor. The saved files are under `~/.local/state/bcp-openproject/stages/<stage-id>/rollback/`. Restoring the data snapshot matters when an OpenProject release has migrated the database schema.

## Proxy, mail, and acceptance

In NPM, add a proxy host for `OPENPROJECT_HOSTNAME`, forward HTTP to the TrueNAS LAN address and `APP_PORT`, and enable WebSocket support. Point the existing Cloudflare DNS record or tunnel at that NPM host. Keep TLS on NPM and set Cloudflare's SSL mode to Full (strict). Do not expose port 8096 directly to the public internet.

Optional SMTP settings use OpenProject's environment aliases. Uncomment and fill `EMAIL_DELIVERY_METHOD=smtp`, `SMTP_ADDRESS`, and any needed port, domain, authentication, TLS, username, and password entries in the protected env file. A password is kept in the private rendered app config and rollback render.

After the first start, sign in as `admin` with the protected initial password from the env file and change it when prompted. Check that:

- `https://OPENPROJECT_HOSTNAME` loads without redirect loops or mixed-content errors.
- You can create a project and work package, upload an attachment, and download it again.
- Two signed-in browser sessions can edit the same rich-text description and see changes appear in both.
- If SMTP is configured, the administration email test succeeds.

## Focused checks

Run the deployment checks and validate Compose with the installed Docker Compose CLI:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s deploy/bcp -p 'test_*.py' -v
BCP_RUNTIME_ENV_FILE="$PWD/deploy/bcp/env.fixture" \
  IMAGE_REF='ghcr.io/mate-vasarhelyi/blindchicken-project:sha-<40-hex-commit>@sha256:<64-hex-digest>' \
  docker compose --env-file deploy/bcp/env.fixture -f deploy/bcp/compose.yaml config --quiet
bash -n deploy/bcp/upstream-release.sh
```

The env fixture contains fake values for validation only. Never use it to create or update an app.
