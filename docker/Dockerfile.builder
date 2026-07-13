# graphify builder — runs `graphify extract .` against an OpenAI-compatible API.
#
# Build:  docker build -f docker/Dockerfile.builder -t graphify-builder .
# Run:    docker run --rm -v "${PWD}:/repo" -e OPENAI_API_KEY -e OPENAI_BASE_URL \
#             graphify-builder /repo --backend openai

FROM python:3.12-slim AS builder-base

WORKDIR /app
COPY . /app

# The [openai] extra pulls `openai` + `tiktoken` (for accurate token counting).
# Base deps (tree-sitter parsers, networkx, numpy, rapidfuzz) are pulled
# transitively.  [mcp] is NOT pulled — the builder never serves HTTP.
RUN pip install --no-cache-dir ".[openai]"

RUN useradd --create-home --uid 10001 graphify
USER graphify

# The caller MUST supply OPENAI_API_KEY.  OPENAI_BASE_URL defaults to
# api.openai.com/v1; set it to point at any OpenAI-compatible endpoint
# (vLLM, LiteLLM proxy, llama.cpp server, Azure Gateway, etc.).
# Model can be overridden via OPENAI_MODEL or GRAPHIFY_OPENAI_MODEL.
ENTRYPOINT ["python", "-m", "graphify"]
CMD ["extract", "/repo", "--backend", "openai"]
