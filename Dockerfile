# Built by CI (.github/workflows/ci.yml), not on a laptop.
# Run: docker run --rm -v "$PWD/outputs:/app/outputs" -e ANTHROPIC_API_KEY -e JEV_API_KEY <image> ingest documents/
# Keys are passed at run time and never baked into the image.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.10.9 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev
COPY documents ./documents
COPY questions.json ./
ENV PATH="/app/.venv/bin:$PATH"
ENTRYPOINT ["abstraction"]
CMD ["--help"]
