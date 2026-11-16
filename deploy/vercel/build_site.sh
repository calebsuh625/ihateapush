#!/bin/zsh
# Build deploy/vercel/_site: Linux (manylinux, Python 3.12) wheels for the hosted API, minus tests and caches.
# Run before `vercel deploy` whenever site-requirements.txt changes.
set -e
cd "${0:A:h}"
rm -rf _site
../../.venv/bin/pip install -q --target _site --platform manylinux_2_28_x86_64 --python-version 3.12 \
  --implementation cp --only-binary=:all: -r site-requirements.txt
find _site -type d \( -name tests -o -name __pycache__ \) -prune -exec rm -rf {} +
rm -rf _site/bin
du -sh _site
