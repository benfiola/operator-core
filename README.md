# operator-core

This repository holds the source code for the `bfiola-operator-core` package that contains utilities for writing python-based kubernetes operators.

---

## Features

- Kubernetes friendly pydantic base models
- Model helper methods:
  - Calculates diffs between models
  - Filters immutable fields
  - Applies diffs for incremental updates
- Decorator-based hook system to register operator methods with resource events
- Base operator class that:
  - Creates a kubernetes client
  - Performs operator login
  - Provides helper methods to operate against the kubernetes api
  - Exposes a healthcheck endpoint
- Utilities:
  - Run sync methods aynchronously
  - Derive resource namespace names
  - Derive resource 'fully-qualified' names

## Usage

This package is hosted on [pypi](https://pypi.org/project/bfiola-operator-core/) and can be installed with the following command:

```shell
pip install bfiola-operator-core
```

A full-featured example can be found in the [minio-operator-ext repository](https://github.com/benfiola/minio-operator-ext/blob/main/minio_operator_ext/operator.py)

A simple example can be found in the [sample launch script](./dev.template.py).

## Development

I personally use [vscode](https://code.visualstudio.com/) as an IDE. For a consistent development experience, this project is also configured to utilize [devcontainers](https://containers.dev/). If you're using both - and you have the [Dev Containers extension](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers) installed - you can follow the [introductory docs](https://code.visualstudio.com/docs/devcontainers/tutorial) to quickly get started.

NOTE: Helper scripts are written under the assumption that they're being executed within a dev container.

### Creating a cluster

From the project root, run the following to create a development cluster to test the operator with:

```shell
cd /workspaces/operator-core
./scripts/dev.sh
```

This will:

- Delete an existing dev cluster if one exists
- Create a new dev cluster

### Creating a launch script

Copy the [dev.template.py](./dev.template.py) script to `dev.py`, then run it to start a sample operator.

If placed in the top-level directory, `dev.py` is gitignored and you can change this file as needed without worrying about committing it to git.

Additionally, the devcontainer is configured with vscode launch configurations that point to a top-level `dev.py` file. You should be able to launch (and attach a debugger to) the operator by launching it natively through vscode.
