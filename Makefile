VENV  := verifier/.venv
PY    := $(VENV)/bin/python
STAMP := $(VENV)/.installed

# A venv is healthy only if it imports both packages from *this*
# checkout. Venvs hardcode absolute paths, so a moved or re-cloned repo
# leaves one that exists but imports nothing (docs/pitfalls.md P-001).
VENV_OK := $(PY) -c 'import sys, vouch_verifier, vouch_harness; \
	sys.exit(not vouch_verifier.__file__.startswith("$(CURDIR)/"))'

.PHONY: test test-go test-py test-harness fuzz integration lint lint-go lint-py fmt cover build install-py eval golden readme readme-check agent eval-real clean

test: test-go test-py test-harness

test-go:
	cd proxy && go vet ./... && go test -race ./...

# build: the differential test runs the Go canonicalizer (vouch canon).
test-py: install-py build
	cd verifier && ../$(PY) -m pytest -q

# The proxy against the official MCP reference server, at a pinned
# version (docs/roadmap.md Phase 5). Needs Node; install scripts are not
# run. CI runs this target.
MCP_EVERYTHING_VERSION := 2026.8.31
MCP_EVERYTHING_DIR := .cache/mcp-reference/$(MCP_EVERYTHING_VERSION)
MCP_EVERYTHING := $(MCP_EVERYTHING_DIR)/node_modules/@modelcontextprotocol/server-everything/dist/index.js
$(MCP_EVERYTHING):
	npm install --prefix $(MCP_EVERYTHING_DIR) --ignore-scripts --no-audit --no-fund \
		@modelcontextprotocol/server-everything@$(MCP_EVERYTHING_VERSION)
integration: $(MCP_EVERYTHING)
	cd proxy && VOUCH_MCP_EVERYTHING="node '$(CURDIR)/$(MCP_EVERYTHING)' stdio" \
		go test -race -count=1 -v ./internal/integration

# Grow the Go fuzz corpus, then check the new entries against Python.
# Commit what lands in proxy/internal/receipt/testdata/fuzz.
FUZZTIME ?= 2m
fuzz: install-py build
	cd proxy && go test ./internal/receipt -run '^$$' -fuzz FuzzCanonicalize -fuzztime $(FUZZTIME)
	cp "$$(cd proxy && go env GOCACHE)"/fuzz/github.com/jma49/vouch/proxy/internal/receipt/FuzzCanonicalize/* \
		proxy/internal/receipt/testdata/fuzz/FuzzCanonicalize/ 2>/dev/null || true
	cd verifier && VOUCH_DIFF_EXAMPLES=2000 ../$(PY) -m pytest -q tests/test_differential.py

test-harness: install-py
	cd harness && ../$(PY) -m pytest -q

lint: lint-go lint-py

# gofmt -l exits 0 even when it lists files; fail on any output.
lint-go:
	cd proxy && test -z "$$(gofmt -l .)" || { gofmt -l .; exit 1; }
	cd proxy && go vet ./...

lint-py: install-py
	$(VENV)/bin/ruff check verifier harness
	$(VENV)/bin/ruff format --check verifier harness
	cd verifier && ../$(VENV)/bin/mypy
	cd harness && ../$(VENV)/bin/mypy

# Format first, then apply lint autofixes. --exit-zero keeps an
# unfixable finding from aborting the target before the rest has run;
# `make lint` is the gate that reports it.
fmt: install-py
	cd proxy && gofmt -w .
	$(VENV)/bin/ruff format verifier harness
	$(VENV)/bin/ruff check --fix --exit-zero verifier harness
	$(VENV)/bin/ruff format verifier harness

# Coverage is reported, not gated (docs/roadmap.md Phase 0).
cover: install-py
	cd proxy && go test -coverprofile=coverage.out ./... >/dev/null && go tool cover -func=coverage.out | tail -1
	cd verifier && ../$(PY) -m pytest -q --cov=vouch_verifier --cov-report=term-missing:skip-covered
	cd harness && ../$(PY) -m pytest -q --cov=vouch_harness --cov-report=term-missing:skip-covered

build:
	cd proxy && go build -o bin/vouch ./cmd/vouch

# Rebuilds the venv when it is broken, reinstalls when a pyproject
# changed, and is a no-op otherwise.
install-py:
	@if [ -f $(STAMP) ] && ! $(VENV_OK) 2>/dev/null; then \
		echo "install-py: venv is stale, rebuilding"; rm -rf $(VENV); fi
	@if [ ! -x $(PY) ]; then python3 -m venv $(VENV); fi
	@if [ ! -f $(STAMP) ] || [ verifier/pyproject.toml -nt $(STAMP) ] \
			|| [ harness/pyproject.toml -nt $(STAMP) ]; then \
		$(PY) -m pip install -q -e "verifier[dev]" -e "harness[dev]" && touch $(STAMP); fi

# Regenerate the Go-produced golden receipt log that the Python tests read.
golden:
	cd proxy && go test ./internal/store/ -run TestGoldenLog -update

eval: install-py
	$(VENV)/bin/vouch-eval --receipts testdata/receipts_golden.jsonl --n 10 \
		--tolerances tolerance.yaml --public-key testdata/keys/golden.pub.pem

# Run a real model on the eval task set (docs/roadmap.md Phase 2). Costs
# API calls: check the plan first with `make agent MODEL=... ARGS=--dry-run`.
MODEL ?= gemini-flash
agent: build install-py
	$(VENV)/bin/vouch-agent --model $(MODEL) $(ARGS)

# Score real runs against human labels (docs/labeling.md). LABELER is
# whose labels count as ground truth.
LABELER ?= $(shell ls eval/labels 2>/dev/null | head -1 | sed 's/\.jsonl$$//')
eval-real: install-py
	$(VENV)/bin/vouch-eval-real --labeler $(or $(LABELER),none)

# README metrics and the example report are generated, never hand-edited
# (AGENTS.md invariant 7). readme-check is what CI runs.
readme: install-py
	$(PY) -m vouch_harness.readme README.md

readme-check: install-py
	$(PY) -m vouch_harness.readme README.md --check

clean:
	rm -rf $(VENV) proxy/bin proxy/coverage.out
