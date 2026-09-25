#!/usr/bin/env python3
"""Stage digest-pinned OpenProject images for the Blind Chicken TrueNAS app."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPOSITORY = "mate-vasarhelyi/blindchicken-project"
IMAGE_REPOSITORY = f"ghcr.io/{REPOSITORY}"
WORKFLOW = "bcp-image.yml"
APP_NAME = "openproject"
ROOT = Path(__file__).resolve().parent
COMPOSE = ROOT / "compose.yaml"
DEFAULT_ENV = Path.home() / ".config" / "bcp-openproject.env"
RUNTIME_KEYS = {
    "SECRET_KEY_BASE",
    "EMAIL_DELIVERY_METHOD",
    "SMTP_ADDRESS",
    "SMTP_PORT",
    "SMTP_DOMAIN",
    "SMTP_AUTHENTICATION",
    "SMTP_ENABLE_STARTTLS_AUTO",
    "SMTP_SSL",
    "SMTP_USER_NAME",
    "SMTP_PASSWORD",
}
SMTP_KEYS = RUNTIME_KEYS - {"SECRET_KEY_BASE", "EMAIL_DELIVERY_METHOD"}
ALLOWED_KEYS = RUNTIME_KEYS | {"OPENPROJECT_HOSTNAME", "DATA_ROOT", "APP_PORT"}
IMAGE_PATTERN = re.compile(
    rf"^{re.escape(IMAGE_REPOSITORY)}:sha-([0-9a-f]{{40}})@(sha256:[0-9a-f]{{64}})$"
)
HOST_PATTERN = re.compile(
    r"^(?=.{1,253}$)(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+"
    r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$"
)
STAGE_ID_PATTERN = re.compile(r"^[0-9]{8}T[0-9]{12}Z-[0-9a-f]{8}$")


class DeployError(Exception):
    pass


def state_root() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    base_info = base.lstat()
    if (
        not stat.S_ISDIR(base_info.st_mode)
        or base_info.st_uid != os.getuid()
        or stat.S_IMODE(base_info.st_mode) & 0o022
    ):
        raise DeployError("state home must be owned by you and not group- or world-writable")
    root = base / "bcp-openproject"
    root.mkdir(mode=0o700, exist_ok=True)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise DeployError("deployment state directory must be owned by you and mode 0700")
    return root


def write_private(path: Path, content: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        output.write(content)


def command(args: list[str], *, env: dict[str, str] | None = None) -> str:
    try:
        result = subprocess.run(args, check=True, text=True, capture_output=True, env=env)
    except (OSError, subprocess.CalledProcessError) as error:
        executable = args[0] if args else "command"
        raise DeployError(f"{executable} failed; output was withheld") from error
    return result.stdout


def remote_output(remote_command: str, payload: str | None = None) -> str:
    try:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "blindchicken-nas", remote_command],
            input=payload,
            check=True,
            text=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise DeployError("TrueNAS command failed; output was withheld") from error
    return result.stdout


def midclt_command(method: str, *params: object, job: bool = False) -> str:
    args = ["sudo", "-n", "midclt", "call"]
    if job:
        args.append("-j")
    args.append(method)
    args.extend(json.dumps(param, separators=(",", ":")) for param in params)
    return " ".join(shlex.quote(arg) for arg in args)


def truenas_call(method: str, *params: object, job: bool = False) -> object:
    output = remote_output("sh -s", midclt_command(method, *params, job=job))
    try:
        return json.loads(output)
    except json.JSONDecodeError as error:
        raise DeployError("TrueNAS middleware returned an invalid response") from error


def read_env(path: Path, *, require_private: bool = True) -> dict[str, str]:
    if path.is_symlink() or not path.is_file():
        raise DeployError("environment file must be a regular file")
    info = path.stat()
    if require_private and (
        info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise DeployError("environment file must be owned by you and mode 0600 or stricter")

    values: dict[str, str] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise DeployError(f"invalid environment entry on line {number}")
        if key not in ALLOWED_KEYS:
            raise DeployError(f"unsupported environment key {key} on line {number}")
        if key in values:
            raise DeployError(f"duplicate environment key {key}")
        if value != value.strip() or value.startswith(("'", '"')) or value.endswith(("'", '"')):
            raise DeployError(f"use an unquoted single-line value for {key}")
        values[key] = value
    return values


def validate_env(values: dict[str, str]) -> dict[str, str]:
    for key in ("OPENPROJECT_HOSTNAME", "DATA_ROOT", "SECRET_KEY_BASE"):
        if not values.get(key):
            raise DeployError(f"missing required environment value: {key}")

    hostname = values["OPENPROJECT_HOSTNAME"]
    if not HOST_PATTERN.fullmatch(hostname):
        raise DeployError("OPENPROJECT_HOSTNAME must be a DNS hostname without a scheme or port")

    data_root = Path(values["DATA_ROOT"])
    if (
        not data_root.is_absolute()
        or str(data_root) != values["DATA_ROOT"]
        or any(character.isspace() for character in values["DATA_ROOT"])
        or len(data_root.parts) < 4
        or data_root.parts[:2] != ("/", "mnt")
        or any(part in ("", ".", "..", ".snapshot") for part in data_root.parts[2:])
    ):
        raise DeployError("DATA_ROOT must be a normalized path below /mnt/<pool>")

    secret = values["SECRET_KEY_BASE"]
    if len(secret) < 64 or not re.fullmatch(r"[a-fA-F0-9]+", secret) or "replace" in secret.lower():
        raise DeployError("SECRET_KEY_BASE must be at least 64 hexadecimal characters")

    port = values.get("APP_PORT", "8096")
    if not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise DeployError("APP_PORT must be between 1 and 65535")

    smtp_values = {key: value for key, value in values.items() if key in SMTP_KEYS and value}
    delivery_method = values.get("EMAIL_DELIVERY_METHOD", "")
    if smtp_values or delivery_method == "smtp":
        if delivery_method != "smtp" or not smtp_values.get("SMTP_ADDRESS"):
            raise DeployError("SMTP requires EMAIL_DELIVERY_METHOD=smtp and SMTP_ADDRESS")
        if "SMTP_PORT" in smtp_values and (
            not smtp_values["SMTP_PORT"].isdecimal()
            or not 1 <= int(smtp_values["SMTP_PORT"]) <= 65535
        ):
            raise DeployError("SMTP_PORT must be between 1 and 65535")
        for key in ("SMTP_ENABLE_STARTTLS_AUTO", "SMTP_SSL"):
            if key in smtp_values and smtp_values[key].lower() not in ("true", "false"):
                raise DeployError(f"{key} must be true or false")
    elif delivery_method:
        raise DeployError("EMAIL_DELIVERY_METHOD is optional and must be smtp when SMTP is configured")

    return values


def parse_image(reference: str) -> tuple[str, str]:
    match = IMAGE_PATTERN.fullmatch(reference.lower())
    if not match:
        raise DeployError("image must be the fork's sha-<commit>@sha256:<digest> reference")
    return match.group(1), match.group(2)


def verify_public_image(reference: str) -> None:
    _, digest = parse_image(reference)
    query = urllib.parse.urlencode({
        "service": "ghcr.io",
        "scope": f"repository:{REPOSITORY}:pull",
    })
    token_url = f"https://ghcr.io/token?{query}"
    manifest_url = f"https://ghcr.io/v2/{REPOSITORY}/manifests/{digest}"
    accepted = ", ".join((
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ))
    try:
        with urllib.request.urlopen(token_url, timeout=20) as response:
            token = json.load(response).get("token")
        request = urllib.request.Request(
            manifest_url,
            headers={"Authorization": f"Bearer {token}", "Accept": accepted},
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            actual_digest = response.headers.get("Docker-Content-Digest")
    except Exception as error:
        raise DeployError("GHCR image must be publicly pullable by TrueNAS") from error
    if actual_digest != digest:
        raise DeployError("GHCR did not return the staged image digest")


def image_from_artifact(run_id: str) -> dict[str, str]:
    run = json.loads(
        command([
            "gh", "run", "view", run_id, "--repo", REPOSITORY,
            "--json", "conclusion,event,headBranch,headSha,status,workflowName",
        ])
    )
    if (
        run.get("workflowName") != "BCP image"
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or run.get("headBranch") != "main"
        or run.get("event") not in ("push", "workflow_dispatch")
    ):
        raise DeployError("deployment requires a successful BCP image run from main")
    if not re.fullmatch(r"[0-9a-f]{40}", run.get("headSha", "")):
        raise DeployError("workflow run has an invalid commit")

    with tempfile.TemporaryDirectory(prefix="bcp-image-") as temporary:
        command([
            "gh", "run", "download", run_id, "--repo", REPOSITORY,
            "--name", "image-reference", "--dir", temporary,
        ])
        artifact_path = Path(temporary) / "image-reference.json"
        try:
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise DeployError("workflow artifact is missing or invalid") from error

    commit = run["headSha"]
    image = validate_artifact(artifact, commit)
    return {"run_id": str(run_id), "commit": commit, "image": image}


def validate_artifact(artifact: dict[str, object], commit: str) -> str:
    digest = str(artifact.get("digest", "")).lower()
    image = str(artifact.get("image", "")).lower()
    expected = f"{IMAGE_REPOSITORY}:sha-{commit}@{digest}"
    if (
        artifact.get("repository") != REPOSITORY
        or artifact.get("commit") != commit
        or artifact.get("ref") != "refs/heads/main"
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
        or image != expected
    ):
        raise DeployError("workflow artifact repository, commit, or digest did not validate")
    return image


def image_from_reference(reference: str) -> dict[str, str]:
    commit, _ = parse_image(reference)
    runs = json.loads(
        command([
            "gh", "run", "list", "--repo", REPOSITORY, "--workflow", WORKFLOW,
            "--commit", commit, "--limit", "100",
            "--json", "conclusion,event,headBranch,headSha,status,workflowName,databaseId",
        ])
    )
    for run in runs:
        if (
            run.get("workflowName") == "BCP image"
            and run.get("status") == "completed"
            and run.get("conclusion") == "success"
            and run.get("headBranch") == "main"
            and run.get("headSha") == commit
            and run.get("event") in ("push", "workflow_dispatch")
        ):
            try:
                result = image_from_artifact(str(run["databaseId"]))
            except DeployError:
                continue
            if result["image"] == reference.lower():
                return result
    raise DeployError("digest does not match an image artifact from a successful main run")


def render_compose(env_path: Path, values: dict[str, str], image: str) -> str:
    runtime = {"SECRET_KEY_BASE": values["SECRET_KEY_BASE"]}
    for key in sorted(SMTP_KEYS | {"EMAIL_DELIVERY_METHOD"}):
        if values.get(key):
            runtime[key] = values[key]

    root = state_root()
    fd, temporary = tempfile.mkstemp(prefix="runtime-", dir=root, text=True)
    runtime_path = Path(temporary)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        output.write("".join(f"{key}={value}\n" for key, value in runtime.items()))

    compose_env = os.environ.copy()
    compose_env["IMAGE_REF"] = image
    compose_env["BCP_RUNTIME_ENV_FILE"] = str(runtime_path)
    try:
        return command([
            "docker", "compose", "--project-directory", str(ROOT),
            "--env-file", str(env_path), "-f", str(COMPOSE), "config",
        ], env=compose_env)
    finally:
        runtime_path.unlink(missing_ok=True)


def stage(args: argparse.Namespace) -> None:
    env_path = args.env_file.expanduser()
    values = validate_env(read_env(env_path))
    env_path = env_path.resolve()
    source = image_from_artifact(args.run) if args.run else image_from_reference(args.image)
    verify_public_image(source["image"])
    root = state_root()
    yaml_text = render_compose(env_path, values, source["image"])
    if str(root) in yaml_text:
        raise DeployError("rendered Compose unexpectedly contains a local state path")

    yaml_hash = hashlib.sha256(yaml_text.encode()).hexdigest()
    stage_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + yaml_hash[:8]
    directory = root / "stages" / stage_id
    directory.mkdir(mode=0o700, parents=True)
    write_private(directory / "compose.yaml", yaml_text)
    manifest = {
        "stage": stage_id,
        "repository": REPOSITORY,
        "commit": source["commit"],
        "run_id": source["run_id"],
        "image": source["image"],
        "yaml_sha256": yaml_hash,
        "data_root": values["DATA_ROOT"],
        "hostname": values["OPENPROJECT_HOSTNAME"],
        "port": values.get("APP_PORT", "8096"),
    }
    write_private(directory / "manifest.json", json.dumps(manifest, indent=2) + "\n")
    print(f"Stage: {stage_id}")
    print(f"Commit: {source['commit']}")
    print(f"Image: {source['image']}")
    print(f"Rendered Compose: {directory / 'compose.yaml'} (mode 0600)")
    print(f"Review SHA-256: {yaml_hash}")
    print("The rendered file contains secrets and was not printed.")


def load_stage(stage_id: str) -> tuple[Path, dict[str, object], str]:
    if not STAGE_ID_PATTERN.fullmatch(stage_id):
        raise DeployError("invalid stage id")
    directory = state_root() / "stages" / stage_id
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        yaml_text = (directory / "compose.yaml").read_text(encoding="utf-8")
    except (OSError, json.JSONDecodeError) as error:
        raise DeployError("stage files are missing or invalid") from error
    digest = hashlib.sha256(yaml_text.encode()).hexdigest()
    try:
        commit, _ = parse_image(str(manifest.get("image", "")))
    except DeployError as error:
        raise DeployError("staged image reference is invalid") from error
    if (
        manifest.get("stage") != stage_id
        or manifest.get("repository") != REPOSITORY
        or manifest.get("commit") != commit
        or manifest.get("yaml_sha256") != digest
    ):
        raise DeployError("staged Compose or manifest changed after it was rendered")
    return directory, manifest, yaml_text


def check_data_root(data_root: str) -> str:
    datasets = remote_output("sudo -n zfs list -H -t filesystem -o name,mountpoint")
    rows = [line.split() for line in datasets.splitlines()]
    dataset = next((row[0] for row in rows if len(row) == 2 and row[1] == data_root), None)
    if not dataset:
        raise DeployError("DATA_ROOT must be the mountpoint of a TrueNAS ZFS dataset")
    try:
        for child in ("postgres", "assets"):
            remote_output("sudo -n test -d " + shlex.quote(f"{data_root}/{child}"))
    except DeployError as error:
        raise DeployError("DATA_ROOT/postgres and DATA_ROOT/assets must already exist") from error
    return dataset


def require_dataset_snapshot(dataset: str, snapshot: str) -> None:
    if not snapshot.startswith(dataset + "@"):
        raise DeployError("backup snapshot must belong to the OpenProject data dataset")
    existing = remote_output(
        "sudo -n zfs list -H -t snapshot -o name " + shlex.quote(dataset)
    ).splitlines()
    if snapshot not in existing:
        raise DeployError("the named TrueNAS backup snapshot does not exist")


def app_entries() -> list[dict[str, object]]:
    result = truenas_call("app.query", [["id", "=", APP_NAME]])
    if not isinstance(result, list):
        raise DeployError("TrueNAS app query returned an unexpected result")
    return [entry for entry in result if isinstance(entry, dict) and entry.get("id") == APP_NAME]


def private_directory(path: Path, *, create: bool = False) -> Path:
    if path.is_symlink():
        raise DeployError(f"private state directory is a symlink: {path.name}")
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    try:
        info = path.lstat()
    except OSError as error:
        raise DeployError(f"private state directory is missing: {path.name}") from error
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise DeployError(f"private state directory must be owned by you and mode 0700: {path.name}")
    return path


def read_current() -> tuple[Path, dict[str, object], str]:
    directory = private_directory(state_root() / "current")
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        yaml_text = (directory / "compose.yaml").read_text(encoding="utf-8")
        commit, _ = parse_image(str(manifest.get("image", "")))
    except (OSError, json.JSONDecodeError, DeployError) as error:
        raise DeployError("no valid previous BCP deployment is recorded locally") from error
    if (
        manifest.get("repository") != REPOSITORY
        or manifest.get("commit") != commit
        or manifest.get("yaml_sha256") != hashlib.sha256(yaml_text.encode()).hexdigest()
    ):
        raise DeployError("previous deployment record is inconsistent")
    return directory, manifest, yaml_text


def replace_private(path: Path, content: str) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent, text=True)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        output.write(content)
    os.replace(temporary, path)


def preserve_previous(stage_dir: Path, manifest: dict[str, object], yaml_text: str) -> Path:
    rollback = stage_dir / "rollback"
    if rollback.exists() or rollback.is_symlink():
        raise DeployError("rollback render already exists for this stage; stage it again before retrying")
    rollback.mkdir(mode=0o700)
    write_private(rollback / "compose.yaml", yaml_text)
    write_private(rollback / "manifest.json", json.dumps(manifest, indent=2) + "\n")
    return rollback


def store_current(manifest: dict[str, object], yaml_text: str) -> None:
    current = private_directory(state_root() / "current", create=True)
    replace_private(current / "compose.yaml", yaml_text)
    replace_private(current / "manifest.json", json.dumps(manifest, indent=2) + "\n")


def validate_backup_marker(marker: dict[str, object], stage_id: str, yaml_hash: str) -> str:
    snapshot = marker.get("snapshot")
    if (
        marker.get("stage") != stage_id
        or marker.get("yaml_sha256") != yaml_hash
        or marker.get("reviewed") is not True
        or not isinstance(snapshot, str)
        or not snapshot
    ):
        raise DeployError("backup marker does not match this reviewed stage")
    return snapshot


def acknowledge_backup(args: argparse.Namespace) -> None:
    if not app_entries():
        raise DeployError("backup markers apply only to an existing openproject app")
    directory, manifest, _ = load_stage(args.stage)
    digest = str(manifest["yaml_sha256"])
    if args.review_sha256 != digest:
        raise DeployError("review SHA-256 does not match this staged Compose")
    snapshot = args.snapshot.strip()
    if not snapshot or len(snapshot) > 256 or any(ord(character) < 32 for character in snapshot):
        raise DeployError("provide a short TrueNAS snapshot identifier")
    dataset = check_data_root(str(manifest["data_root"]))
    require_dataset_snapshot(dataset, snapshot)
    marker = {
        "stage": args.stage,
        "yaml_sha256": digest,
        "snapshot": snapshot,
        "reviewed": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    marker_path = directory / "backup-marker.json"
    if marker_path.exists():
        raise DeployError("backup marker already exists for this stage")
    write_private(marker_path, json.dumps(marker, indent=2) + "\n")
    print(f"Backup marker recorded for stage {args.stage} and snapshot {snapshot}.")


def deploy_stage(args: argparse.Namespace, *, create: bool) -> None:
    stage_dir, manifest, yaml_text = load_stage(args.stage)
    image = str(manifest["image"])
    if args.confirm is not True:
        raise DeployError("pass the explicit confirmation flag to create or apply")
    dataset = check_data_root(str(manifest["data_root"]))
    apps = app_entries()
    current_dir = state_root() / "current"

    if create:
        if apps:
            raise DeployError("an app named openproject already exists")
        if current_dir.exists() or current_dir.is_symlink():
            raise DeployError("local deployment state already exists; refusing to recreate")
        occupied = remote_output(
            f"sudo -n ss -H -ltn 'sport = :{int(manifest['port'])}'"
        ).strip()
        if occupied:
            raise DeployError("configured APP_PORT is already in use")
        method = "app.create"
        payload = {
            "app_name": APP_NAME,
            "custom_app": True,
            "custom_compose_config_string": yaml_text,
        }
        rollback = None
    else:
        if len(apps) != 1:
            raise DeployError("an existing openproject app is required for apply")
        app = apps[0]
        if app.get("custom_app") is not True or app.get("state") != "RUNNING":
            raise DeployError("apply requires a running TrueNAS Custom App named openproject")
        try:
            marker = json.loads((stage_dir / "backup-marker.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise DeployError("apply requires a backup marker for this exact staged Compose") from error
        validate_backup_marker(marker, args.stage, str(manifest["yaml_sha256"]))
        require_dataset_snapshot(dataset, str(marker["snapshot"]))
        _, current_manifest, current_yaml = read_current()
        current_digest = parse_image(str(current_manifest["image"]))[1]
        workloads = app.get("active_workloads")
        active_images = workloads.get("images", []) if isinstance(workloads, dict) else []
        if not isinstance(active_images, list) or not any(
            isinstance(active_image, str)
            and active_image.startswith(IMAGE_REPOSITORY)
            and active_image.endswith("@" + current_digest)
            for active_image in active_images
        ):
            raise DeployError("TrueNAS is not running the image in the local rollback record")
        rollback = preserve_previous(stage_dir, current_manifest, current_yaml)
        rollback_image = str(current_manifest["image"])
        payload = {
            "app_name": APP_NAME,
            "update": {"custom_compose_config_string": yaml_text},
        }
        method = "app.update"
    truenas_call(method, payload, job=True)
    store_current(manifest, yaml_text)
    print(f"{'Created' if create else 'Applied'} TrueNAS app {APP_NAME}.")
    print(f"Image: {image}")
    if rollback:
        print(f"Previous image: {rollback_image}")
        print(f"Previous render: {rollback / 'compose.yaml'}")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)

    stage_parser = commands.add_parser("stage", help="render and review a successful main image")
    source = stage_parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--run", help="successful BCP image workflow run id")
    source.add_argument("--image", help="digest reference emitted by a successful main run")
    stage_parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)

    marker = commands.add_parser("acknowledge-backup", help="record a reviewed backup for an update")
    marker.add_argument("--stage", required=True)
    marker.add_argument("--review-sha256", required=True)
    marker.add_argument("--snapshot", required=True)

    for name, confirm in (("create", "--confirm-create"), ("apply", "--confirm-apply")):
        deploy = commands.add_parser(name, help=f"{name} the staged app image")
        deploy.add_argument("--stage", required=True)
        deploy.add_argument(confirm, dest="confirm", action="store_true")

    return result


def main() -> None:
    args = parser().parse_args()
    try:
        if args.command == "stage":
            stage(args)
        elif args.command == "acknowledge-backup":
            acknowledge_backup(args)
        elif args.command in ("create", "apply"):
            deploy_stage(args, create=args.command == "create")
    except DeployError as error:
        raise SystemExit(f"bcp deploy: {error}") from error


if __name__ == "__main__":
    main()
