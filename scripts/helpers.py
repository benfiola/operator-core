#!/usr/bin/env python3
import io
import os
import pathlib
import shlex
import subprocess
import sys
import threading

import click
import packaging.utils
import toml


def run_cmd(cmd: list[str], **kwargs):
    buffer_stdout = io.StringIO()
    buffer_stderr = io.StringIO()

    def _reader(role: str):
        nonlocal buffer_stdout, buffer_stderr, popen
        if role == "stdout":
            in_buffer = popen.stdout
            out_buffer = buffer_stdout
        elif role == "stderr":
            in_buffer = popen.stderr
            out_buffer = buffer_stderr
        else:
            raise NotImplementedError()

        def read():
            if in_buffer is None:
                raise RuntimeError()
            data = in_buffer.read()
            if data is None:
                return
            out_buffer.write(data)
            sys.stderr.write(data)

        while popen.returncode is not None:
            read()
        read()

    cmd_str = f"{shlex.join(cmd)}"
    if env := kwargs.get("env"):
        diff = {}
        for key, value in env.items():
            if os.environ.get(key) == value:
                continue
            diff[key] = value
        diff_str = " ".join(f"{k}={v}" for k, v in diff.items())
        if diff_str:
            cmd_str += f" (env: {diff_str})"
    if cwd := kwargs.get("cwd"):
        cmd_str += f" (cwd: {cwd})"

    print(f"$ {cmd_str}", file=sys.stderr)
    kwargs["encoding"] = "utf-8"
    kwargs["stdout"] = subprocess.PIPE
    kwargs["stderr"] = subprocess.PIPE
    popen = subprocess.Popen(cmd, **kwargs)
    readers = [
        threading.Thread(target=lambda: _reader("stdout")),
        threading.Thread(target=lambda: _reader("stderr")),
    ]
    [r.start() for r in readers]
    popen.wait()
    [r.join() for r in readers]

    buffer_stdout.seek(0)
    buffer_stderr.seek(0)
    stdout = buffer_stdout.read()
    stderr = buffer_stderr.read()
    if popen.returncode != 0:
        raise subprocess.CalledProcessError(
            cmd=cmd, output=stdout, returncode=popen.returncode, stderr=stderr
        )
    return stdout


def main():
    grp_main()


@click.group()
def grp_main():
    pass


@grp_main.command("build")
def cmd_build():
    run_cmd(["python", "-m", "build"])


@grp_main.command("publish")
@click.option("--token")
def cmd_publish(*, token: str):
    pyproject_file = pathlib.Path.cwd().joinpath("pyproject.toml")
    if not pyproject_file.exists():
        raise FileNotFoundError(pyproject_file)
    data = toml.loads(pyproject_file.read_text())
    name = data["project"]["name"]
    version = data["project"]["version"]

    dist_folder = pathlib.Path.cwd().joinpath("dist")
    if not dist_folder.exists():
        raise FileNotFoundError(dist_folder)
    pkg_name = packaging.utils.canonicalize_name(name).replace("-", "_")
    pkg_version = packaging.utils.canonicalize_version(version)
    files = [
        dist_folder.joinpath(f"{pkg_name}-{pkg_version}-py3-none-any.whl"),
        dist_folder.joinpath(f"{pkg_name}-{pkg_version}.tar.gz"),
    ]
    for file in files:
        if not file.exists():
            raise FileNotFoundError(file)
    run_cmd(
        [
            "python",
            "-m",
            "twine",
            "--no-color",
            "upload",
            "--disable-progress-bar",
            "--repository-url=https://upload.pypi.org/legacy/",
            "-u=__token__",
            f"-p={token}",
            *[str(f) for f in files],
        ]
    )


@grp_main.command("print-next-version")
@click.option("--as-tag", is_flag=True)
def cmd_print_next_version(*, as_tag: bool = False):
    command = ["python", "-m", "semantic_release", "--noop", "--strict", "version"]
    if as_tag is True:
        command.extend(["--print-tag"])
    else:
        command.extend(["--print"])
    env = {"GH_TOKEN": "undefined", **os.environ}
    version = run_cmd(command, env=env).strip()
    click.echo(version)


@grp_main.command("set-version")
@click.argument("version")
def cmd_set_version(*, version: str):
    pyproject_file = pathlib.Path.cwd().joinpath("pyproject.toml")
    if not pyproject_file.exists():
        raise FileNotFoundError(pyproject_file)
    data = toml.loads(pyproject_file.read_text())
    data["project"]["version"] = version
    pyproject_file.write_text(toml.dumps(data))


if __name__ == "__main__":
    main()
