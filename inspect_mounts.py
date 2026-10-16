"""Read effective experiment mount settings without the CLI YAML serializer."""
import argparse
import json
import os
from urllib.parse import urlsplit


BROKEN_PATH = "/mount/ccai_nas2/shuai/determined"


def mount_summary(config):
    fields = ("type", "host_path", "container_path", "storage_path", "read_only")

    def select(value):
        return {key: value[key] for key in fields if key in value} if isinstance(value, dict) else None

    matches = []

    def locate(value, path):
        if isinstance(value, dict):
            for key, child in value.items():
                locate(child, f"{path}.{key}" if path else key)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                locate(child, f"{path}[{index}]")
        elif isinstance(value, str) and BROKEN_PATH in value:
            matches.append(path)

    locate(config, "")
    return {
        "checkpoint_storage": select(config.get("checkpoint_storage")),
        "bind_mounts": [select(mount) for mount in (config.get("bind_mounts") or [])],
        "broken_path_config_locations": matches,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("experiment", type=int)
    parser.add_argument("--master", default=os.environ.get("DET_MASTER", "http://10.130.130.5:4096"))
    args = parser.parse_args()
    host = urlsplit(args.master).hostname
    for key in ("NO_PROXY", "no_proxy"):
        os.environ[key] = ",".join(filter(None, [os.environ.get(key, ""), host]))

    from determined.common import api
    from determined.common.api import authentication, bindings, certs

    cert = certs.default_load(args.master)
    auth = authentication.Authentication(args.master, cert=cert)
    session = api.Session(master=args.master, user=auth.get_session_user(), auth=auth, cert=cert)
    experiment = bindings.get_GetExperiment(session, experimentId=args.experiment).experiment
    config = experiment.config
    if not isinstance(config, dict):
        raise TypeError("Expected effective experiment configuration as a JSON object")
    result = {"experiment": args.experiment,
              "state": getattr(experiment.state, "value", str(experiment.state)),
              **mount_summary(config)}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
