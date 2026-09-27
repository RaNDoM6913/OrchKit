"""Check a clean wheel install with -I, outside the source checkout."""

import argparse
import configparser
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_root", type=Path)
    source = parser.parse_args().source_root.resolve()
    require(bool(sys.flags.isolated), "Run this check with Python -I")
    require(sys.prefix != sys.base_prefix, "Use a clean virtual environment")
    require(not Path.cwd().resolve().is_relative_to(source),
            "Run this check outside the source checkout")
    prefix = Path(sys.prefix).resolve()
    config = configparser.ConfigParser()
    with (source / "setup.cfg").open(encoding="utf-8") as stream:
        config.read_file(stream)
    require(config["metadata"]["name"] == "orchkit", "Unexpected distribution name")
    expected_version = config["metadata"]["version"]

    # -I excludes the checkout and PYTHONPATH from package discovery.
    from importlib import metadata, resources
    import orch

    require(Path(orch.__file__).resolve().is_relative_to(prefix),
            "orch was not imported from the smoke virtual environment")
    require(metadata.version("orchkit") == orch.__version__ == expected_version,
            "Source metadata, installed metadata and runtime version disagree")
    template = resources.files("orch").joinpath("templates/dispatcher_prompt.txt").read_bytes()
    require(bool(template), "Installed dispatcher template is empty")
    require(template == (source / "orch/templates/dispatcher_prompt.txt").read_bytes(),
            "Installed dispatcher template differs from the candidate source")
    cli = prefix / "bin/orch"
    require(cli.is_file(), "Installed orch entry point is missing")

    with tempfile.TemporaryDirectory(prefix="orchkit-installed-smoke-") as directory:
        temporary = Path(directory).resolve()
        home = temporary / "orch-home"
        output = temporary / "dispatcher.txt"
        env = os.environ.copy()
        for key in ("PYTHONPATH", "PYTHONHOME"):
            env.pop(key, None)
        env.update(ORCH_HOME=str(home), ORCH_EXECUTABLE=str(cli))
        def run_cli(*arguments: str) -> str:
            return subprocess.run(
                [str(cli), *arguments], cwd=temporary, env=env,
                check=True, capture_output=True, text=True, timeout=30,
            ).stdout

        require(run_cli("--version").strip() == f"orch {expected_version}",
                "Installed CLI version disagrees with candidate metadata")
        result = json.loads(run_cli(
            "--root", str(home), "dispatcher", "render", "--output", str(output),
        ))
        require(result["status"] == "RENDERED", "Dispatcher rendering failed")
        rendered = output.read_text(encoding="utf-8")
        require(bool(rendered) and "{{ORCH_HOME}}" not in rendered,
                "Dispatcher output is empty or has an unresolved home placeholder")
        require(shlex.quote(str(home)) in rendered, "Dispatcher did not use the disposable home")

    print(json.dumps({"status": "PASS", "version": expected_version,
                      "template_bytes": len(template), "dispatcher_render": "PASS"}))


if __name__ == "__main__":
    main()
