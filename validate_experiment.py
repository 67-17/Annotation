"""Ask the master to validate configuration only; never schedule an experiment."""
import argparse
import os
from pathlib import Path

from attach import create_session


def validate(session, config_path, context_path):
    from determined.common import context
    from determined.common.api import bindings

    request = bindings.v1CreateExperimentRequest(
        config=Path(config_path).read_text(),
        modelDefinition=context.read_v1_context(Path(context_path), []),
        activate=False,
        validateOnly=True,
    )
    return bindings.post_CreateExperiment(session, body=request)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config")
    parser.add_argument("context")
    parser.add_argument("--master", default=os.environ.get("DET_MASTER", "http://10.130.130.5:4096"))
    args = parser.parse_args()
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(name, None)
    validate(create_session(args.master), args.config, args.context)
    print("CONFIG_VALIDATED: master accepted the configuration; no experiment created or scheduled.")


if __name__ == "__main__":
    main()
