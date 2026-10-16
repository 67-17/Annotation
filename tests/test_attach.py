"""Offline authentication compatibility checks; never reads credentials or connects."""
import unittest
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest import mock

from determined.common import api
from determined.common.api import authentication, bindings, certs

from scripts.attach import create_session, diagnose_proxy
from scripts.validate_experiment import validate
from scripts import inspect_mounts
from determined.common import context


class SessionTests(unittest.TestCase):
    def test_mount_diagnostics_reuse_compatible_authentication(self):
        experiment = SimpleNamespace(config={"bind_mounts": [], "checkpoint_storage": {"type": "shared_fs"}}, state="STATE_RUNNING")
        with mock.patch("sys.argv", ["inspect_mounts.py", "123"]), \
             mock.patch.object(inspect_mounts, "create_session", return_value=mock.sentinel.session) as login, \
             mock.patch.object(bindings, "get_GetExperiment", return_value=SimpleNamespace(experiment=experiment)) as get, \
             mock.patch("builtins.print"):
            inspect_mounts.main()
        login.assert_called_once_with("http://10.130.130.5:4096")
        get.assert_called_once_with(mock.sentinel.session, experimentId=123)

    def test_deployment_proxy_field_matches_master_yaml_schema(self):
        from ruamel.yaml import YAML

        yaml = YAML(typ="safe")
        for name in ("experiment.yaml", "command.yaml"):
            with self.subTest(config=name):
                config = yaml.load((Path(__file__).resolve().parent.parent / "configs" / name).read_text())
                self.assertEqual(config["environment"]["proxy_ports"], [
                    {"proxy_port": 8088, "proxy_tcp": True}])
                self.assertEqual(config["environment"]["ports"]["contact_web"], 8088)

    def test_validation_never_activates_or_creates_experiment(self):
        with mock.patch("scripts.validate_experiment.Path.read_text", return_value="name: test"), \
             mock.patch.object(context, "read_v1_context", return_value=[]) as read_context, \
             mock.patch.object(bindings, "post_CreateExperiment") as post:
            validate(mock.sentinel.session, "/test/config.yaml", "/test/context")
        read_context.assert_called_once_with(Path("/test/context"), [])
        request = post.call_args.kwargs["body"]
        self.assertIs(request.validateOnly, True)
        self.assertIs(request.activate, False)
        self.assertEqual(request.config, "name: test")

    def test_validation_reads_real_directory_with_installed_client(self):
        # Exercise the actual client packager: mocking it hid its Path requirement.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "experiment.yaml"
            config.write_text("name: offline-test\n")
            source = root / "source"
            source.mkdir()
            (source / "start.sh").write_text("exit 0\n")
            with mock.patch.object(bindings, "post_CreateExperiment") as post:
                validate(mock.sentinel.session, str(config), str(source))
            post.assert_called_once()
            request = post.call_args.kwargs["body"]
            self.assertEqual([item.path for item in request.modelDefinition], ["start.sh"])
            self.assertIs(request.validateOnly, True)
            self.assertIs(request.activate, False)
            self.assertEqual(request.to_json()["validateOnly"], True)

    def test_diagnostics_filter_unrelated_tasks_and_credentials(self):
        experiment = SimpleNamespace(id=65398, config={"environment": {
            "ports": {"contact_web": 8088}, "proxy_ports": [],
            "environment_variables": ["SECRET=hidden"], "registry_auth": {"password": "hidden"}}})
        trial = SimpleNamespace(id=66777, taskId="selected-task", state="STATE_RUNNING")
        own = SimpleNamespace(taskId="selected-task", to_json=lambda: {
            "taskId": "selected-task", "proxyPorts": [], "resources": []})
        other = SimpleNamespace(taskId="unrelated-task", to_json=lambda: self.fail("Unrelated task read"))
        with mock.patch.object(bindings, "get_GetExperimentTrials", return_value=SimpleNamespace(trials=[trial])), \
             mock.patch.object(bindings, "get_GetTrial", return_value=SimpleNamespace(trial=trial)), \
             mock.patch.object(bindings, "get_GetTasks", return_value=SimpleNamespace(
                 allocationIdToSummary={"selected.1": own, "other.1": other})):
            result = diagnose_proxy(mock.sentinel.session, experiment)
        self.assertEqual(result["active_allocations"][0]["allocation_key"], "selected.1")
        self.assertEqual(len(result["active_allocations"]), 1)
        self.assertNotIn("hidden", json.dumps(result))

    def test_modern_authentication(self):
        with mock.patch.object(certs, "default_load", return_value=mock.sentinel.cert), \
             mock.patch.object(authentication, "login_with_cache", create=True,
                               return_value=mock.sentinel.session) as login, \
             mock.patch.object(authentication, "Authentication", create=True) as legacy:
            self.assertIs(create_session("http://test:4096"), mock.sentinel.session)
            login.assert_called_once_with(master_address="http://test:4096", cert=mock.sentinel.cert)
            legacy.assert_not_called()

    def test_legacy_authentication(self):
        with mock.patch.object(certs, "default_load", return_value=mock.sentinel.cert), \
             mock.patch.object(authentication, "login_with_cache", None, create=True), \
             mock.patch.object(authentication, "Authentication", create=True) as legacy, \
             mock.patch.object(authentication, "cli_auth", None, create=True), \
             mock.patch.object(api, "Session", return_value=mock.sentinel.session) as session:
            legacy.return_value.get_session_user.return_value = "test-user"
            self.assertIs(create_session("http://test:4096"), mock.sentinel.session)
            legacy.assert_called_once_with("http://test:4096", cert=mock.sentinel.cert)
            self.assertIs(authentication.cli_auth, legacy.return_value)
            session.assert_called_once_with(master="http://test:4096", user="test-user",
                                            auth=legacy.return_value, cert=mock.sentinel.cert)


if __name__ == "__main__":
    unittest.main()
