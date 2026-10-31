FROM python:3.11-slim

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

RUN useradd --create-home --uid 10001 mcp
USER mcp

ENV MCP_TRANSPORT=http MCP_PORT=8000
EXPOSE 8000

CMD ["openwebui-rag-mcp"]
