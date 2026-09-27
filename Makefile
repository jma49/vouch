VENV  := verifier/.venv
PY    := $(VENV)/bin/python
STAMP := $(VENV)/.installed

# A venv is healthy only if it imports both packages from *this*
# checkout. Venvs hardcode absolute paths, so a moved or re-cloned repo
# leaves one that exists but imports nothing (docs/pitfalls.md P-001).
VENV_OK := $(PY) -c 'import sys, vouch_verifier, vouch_harness; \
	sys.exit(not vouch_verifier.__file__.startswith("$(CURDIR)/"))'

.PHONY: test test-go test-py test-harness lint build install-py eval golden clean

test: test-go test-py test-harness

test-go:
	cd proxy && go vet ./... && go test ./...

test-py: install-py
	cd verifier && ../$(PY) -m pytest -q

test-harness: install-py
	cd harness && ../$(PY) -m pytest -q

lint:
	cd proxy && gofmt -l . && go vet ./...

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
	VOUCH_HMAC_KEY=vouch-golden-key $(VENV)/bin/vouch-eval \
		--receipts testdata/receipts_golden.jsonl --n 10 --tolerances tolerance.yaml

clean:
	rm -rf $(VENV) proxy/bin
