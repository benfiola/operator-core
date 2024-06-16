#!/bin/sh
set -e
rm -rf ./dist/*
python -m build --sdist
twine upload ./dist/*