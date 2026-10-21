"""Create an experiment config with state isolated for one service instance."""

import argparse
from pathlib import Path


def load_yaml(path):
    try:
        import yaml

        return yaml.safe_load(path.read_text()), lambda value: yaml.safe_dump(value, allow_unicode=True, sort_keys=False)
    except ImportError:
        from ruamel.yaml import YAML

        parser = YAML()
        value = parser.load(path.read_text())

        def dump(data):
            from io import StringIO

            output = StringIO()
            parser.dump(data, output)
            return output.getvalue()

        return value, dump


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("instance")
    args = parser.parse_args()

    config, dump = load_yaml(args.source)
    config["name"] = f'{config["name"]}-{args.instance}'
    variables = config["environment"]["environment_variables"]
    prefix = "ANNOTATION_STATE_DIR="
    state = f"/home/liushiqi/Annotation/state-{args.instance}"
    config["environment"]["environment_variables"] = [
        f"{prefix}{state}" if item.startswith(prefix) else item for item in variables
    ]
    args.destination.write_text(dump(config))


if __name__ == "__main__":
    main()
