.PHONY: test test-go test-py lint

test: test-go test-py

test-go:
	cd proxy && go vet ./... && go test ./...

test-py:
	cd verifier && python -m pytest -q

lint:
	cd proxy && gofmt -l . && go vet ./...
