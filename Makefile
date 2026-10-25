PYTHON ?= python3

.PHONY: build build-all test clean

# Single-file executable for this machine -> dist/zir-<os>-<arch>
build:
	$(PYTHON) scripts/build.py

# macOS (arm64, x86_64) + Linux (x86_64, arm64) + Windows via GitHub Actions -> dist/
build-all:
	$(PYTHON) scripts/build.py --all

test:
	.venv/bin/pytest -q

clean:
	rm -rf build dist .venv-build
