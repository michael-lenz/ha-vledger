# syntax=docker/dockerfile:1
# Operating manual — the toolchain of ha-vledger for autofac: the project
# Dockerfile an instance file names as [container] dockerfile (autofac
# REQ-0011), built from autofac's docs/operating/project.example.Dockerfile.
#
# The syntax line stays first, as Docker reads it only there; the heredoc
# below needs it. The build context is this file's directory, so
# pyproject.toml has to lie beside it. The COPY is hashed into the image's
# tag, so `autofac run` builds the image anew at its next start after the
# dependencies change.
#
# It installs what pyproject.toml names under the dev and ha extras —
# pytest, pytest-asyncio, tzdata, ruff, and Home Assistant with its test
# plugin — and not vledger itself, since every agent works on a checkout of
# its own. The checks therefore run with PYTHONPATH=src from the checkout's
# root, so that the checkout's library is the one imported:
#
#   PYTHONPATH=src python -m pytest -q
#   python -m ruff check src tests custom_components
#   node --test tests/frontend/model.test.mjs
#
# Under Docker the checks run without network, so everything they need is
# installed here, Node 22 for the card's model among it (ADR-0039). Node
# comes from NodeSource with npm, so autofac's own layer finds npm and
# installs no second Node for Claude Code.
#
# autofac builds its own layer over this image (src/autofac/image/
# agent.Dockerfile) and installs with apt what it lacks, so the base is
# Debian. Python 3.13 is the project's floor (requires-python) and what CI
# runs.
#
# build-essential stays in the image: Home Assistant pulls in lru-dict,
# which has no wheel for Python 3.13 and is a C extension, and an agent
# that makes a venv for a branch with changed dependencies needs the
# compiler again.

FROM python:3.13-slim

RUN set -e; \
    apt-get update; \
    apt-get install -y --no-install-recommends build-essential ca-certificates curl git; \
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash -; \
    apt-get install -y --no-install-recommends nodejs; \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml /tmp/pyproject.toml

# The requirements of the dev and ha extras, with the self-references
# (dev = ["vledger[test]", "ruff"]) resolved to the extras they name, so
# that pip never installs vledger from PyPI.
RUN python3 - /tmp/pyproject.toml > /tmp/requirements.txt <<'EOF'
import re
import sys
import tomllib

with open(sys.argv[1], "rb") as file:
    project = tomllib.load(file)["project"]
name = project["name"]
extras = project.get("optional-dependencies", {})
seen = set()

def requirements(extra):
    if extra in seen:
        return
    seen.add(extra)
    for requirement in extras.get(extra, []):
        own = re.fullmatch(rf"{re.escape(name)}\[([^\]]+)\]", requirement.strip())
        if own:
            for inner in own.group(1).split(","):
                yield from requirements(inner.strip())
        else:
            yield requirement

for requirement in project.get("dependencies", []):
    print(requirement)
for extra in ("dev", "ha"):
    for requirement in requirements(extra):
        print(requirement)
EOF

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt /tmp/pyproject.toml
