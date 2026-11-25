# Build: dependencies first (cached layer), then the app.
FROM rust:1-slim-bookworm AS build
WORKDIR /src
COPY Cargo.toml Cargo.lock ./
RUN mkdir src && echo 'fn main() {}' > src/main.rs && cargo build --release \
 && rm -rf src target/release/deps/cypress_tickets* target/release/.fingerprint/cypress-tickets-*
COPY src ./src
# COPY keeps the sources' old mtimes; touch so cargo rebuilds the app instead of reusing the stub.
RUN touch src/*.rs && cargo build --release

# Run: distroless (glibc, no shell), non-root.
FROM gcr.io/distroless/cc-debian12:nonroot
COPY --from=build /src/target/release/cypress-tickets /cypress-tickets
ENV PORT=8080
USER nonroot
ENTRYPOINT ["/cypress-tickets"]
