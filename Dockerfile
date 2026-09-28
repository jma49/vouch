# Build the Go proxy, then land it in a Python image alongside the
# verifier and harness: one image carrying the whole pipeline, since
# the proxy writes receipts the Python side reads from the same volume.
FROM golang:1.27-bookworm AS proxy-build
WORKDIR /src/proxy
COPY proxy/go.mod proxy/go.sum ./
RUN go mod download
COPY proxy/ ./
RUN CGO_ENABLED=0 go build -o /out/vouch ./cmd/vouch

FROM python:3.12-slim
WORKDIR /app
COPY --from=proxy-build /out/vouch /usr/local/bin/vouch
COPY verifier/ ./verifier/
COPY harness/ ./harness/
RUN pip install --no-cache-dir -e ./verifier -e ./harness
COPY schemas/ ./schemas/
COPY tolerance.yaml ./
COPY testdata/ ./testdata/

# Run as an unprivileged user: the proxy spawns upstream servers, and
# they inherit its user (docs/threat-model.md, operating guidance). The
# state directories exist in the image so a new named volume mounted on
# one starts out owned by that user.
RUN useradd --system --uid 10001 --no-create-home --home-dir /nonexistent vouch \
    && mkdir -p /app/receipts /app/fixtures /app/keys \
    && chown vouch:vouch /app/receipts /app/fixtures /app/keys \
    && chmod 0700 /app/keys
USER vouch

# Receipts and fixtures are state: mount them.
VOLUME ["/app/receipts", "/app/fixtures"]
ENTRYPOINT ["vouch"]
CMD ["version"]
