VENV := verifier/.venv
PY   := $(VENV)/bin/python
PIP  := $(VENV)/bin/pip

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

install-py: $(VENV)
$(VENV):
	python3 -m venv $(VENV)
	$(PIP) install -q -e "verifier[dev]" -e "harness[dev]"

# Regenerate the Go-produced golden receipt log that the Python tests read.
golden:
	cd proxy && go test ./internal/store/ -run TestGoldenLog -update

eval: install-py
	VOUCH_HMAC_KEY=vouch-golden-key $(VENV)/bin/vouch-eval \
		--receipts testdata/receipts_golden.jsonl --n 10 --tolerances tolerance.yaml

clean:
	rm -rf $(VENV) proxy/bin
