import json
import os
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import deploy


COMMIT = "a" * 40
DIGEST = "sha256:" + "b" * 64
IMAGE = f"{deploy.IMAGE_REPOSITORY}:sha-{COMMIT}@{DIGEST}"
ENV = {
    "OPENPROJECT_HOSTNAME": "projects.example.test",
    "DATA_ROOT": "/mnt/tank/apps/openproject-test",
    "APP_PORT": "8096",
    "SECRET_KEY_BASE": "0123456789abcdef" * 4,
    "OPENPROJECT_SEED__ADMIN__USER__PASSWORD": "a" * 48,
}


class DeployChecks(unittest.TestCase):
    def test_digest_reference_and_artifact_are_bound_to_repo_commit(self):
        self.assertEqual(deploy.parse_image(IMAGE), (COMMIT, DIGEST))
        artifact = {
            "repository": deploy.REPOSITORY,
            "commit": COMMIT,
            "ref": "refs/heads/main",
            "digest": DIGEST,
            "image": IMAGE,
        }
        self.assertEqual(deploy.validate_artifact(artifact, COMMIT), IMAGE)
        artifact["ref"] = "refs/heads/candidate"
        with self.assertRaises(deploy.DeployError):
            deploy.validate_artifact(artifact, COMMIT)
        with self.assertRaises(deploy.DeployError):
            deploy.parse_image(IMAGE.replace(deploy.REPOSITORY, "somebody/else"))

    def test_env_requires_secret_hostname_and_snapshot_root(self):
        self.assertEqual(deploy.validate_env(dict(ENV)), ENV)
        for key, value in (
            ("SECRET_KEY_BASE", "replace-with-a-secret-that-is-not-real"),
            ("OPENPROJECT_SEED__ADMIN__USER__PASSWORD", "admin"),
            ("OPENPROJECT_HOSTNAME", "https://projects.example.test"),
            ("DATA_ROOT", "/mnt/tank/../outside"),
        ):
            invalid = dict(ENV, **{key: value})
            with self.subTest(key=key), self.assertRaises(deploy.DeployError):
                deploy.validate_env(invalid)

    def test_smtp_is_optional_and_requires_a_complete_method(self):
        self.assertEqual(deploy.validate_env(dict(ENV)), ENV)
        with self.assertRaises(deploy.DeployError):
            deploy.validate_env(dict(ENV, EMAIL_DELIVERY_METHOD="smtp"))
        smtp = dict(
            ENV,
            EMAIL_DELIVERY_METHOD="smtp",
            SMTP_ADDRESS="smtp.example.test",
            SMTP_PORT="587",
            SMTP_ENABLE_STARTTLS_AUTO="true",
        )
        self.assertEqual(deploy.validate_env(smtp), smtp)

    def test_backup_marker_is_bound_to_stage_and_render_hash(self):
        marker = {
            "stage": "20260925T123456123456Z-deadbeef",
            "yaml_sha256": "c" * 64,
            "snapshot": "tank/apps/openproject@before-update",
            "reviewed": True,
        }
        self.assertEqual(
            deploy.validate_backup_marker(marker, marker["stage"], marker["yaml_sha256"]),
            marker["snapshot"],
        )
        with self.assertRaises(deploy.DeployError):
            deploy.validate_backup_marker(marker, marker["stage"], "d" * 64)

    def test_middleware_request_keeps_secrets_out_of_sudo_arguments(self):
        secret = "SECRET_KEY_BASE=private\n; echo unsafe"
        payload = {"custom_compose_config_string": secret}
        with patch.object(deploy, "remote_output", return_value="{}") as remote:
            deploy.truenas_call("app.update", deploy.APP_NAME, payload, job=True)
        command, request = remote.call_args.args
        self.assertEqual(shlex.split(command)[:4], ["sudo", "-n", "python3", "-c"])
        self.assertNotIn(secret, command)
        self.assertEqual(
            json.loads(request),
            {"method": "app.update", "params": [deploy.APP_NAME, payload], "job": True},
        )

    def test_compose_config_renders_a_single_digest_pinned_service(self):
        env_path = Path(__file__).with_name("env.fixture")
        values = deploy.validate_env(deploy.read_env(env_path, require_private=False))
        with tempfile.TemporaryDirectory() as state:
            os.chmod(state, 0o700)
            with patch.dict(os.environ, {"XDG_STATE_HOME": state}):
                rendered = deploy.render_compose(env_path.resolve(), values, IMAGE)
        self.assertIn(IMAGE, rendered)
        self.assertIn("published: \"8096\"", rendered)
        self.assertIn("target: /var/openproject/pgdata", rendered)
        self.assertIn("target: /var/openproject/assets", rendered)
        self.assertNotIn("5432", rendered)
        self.assertNotIn("BCP_RUNTIME_ENV_FILE", rendered)
        self.assertIn(ENV["SECRET_KEY_BASE"], rendered)
        self.assertIn(ENV["OPENPROJECT_SEED__ADMIN__USER__PASSWORD"], rendered)


if __name__ == "__main__":
    unittest.main()
