# syntax=docker/dockerfile:1

FROM python:3.12-slim

# No build tools needed: the core has no compiled dependencies.
RUN pip install --no-cache-dir "gemini-bridge[full]"

# Copy the package only. The image deliberately contains no config.json and no
# session cookie: mount both at runtime so the image stays credential-free and
# can be published without leaking anything.
WORKDIR /app
COPY gemini_bridge ./gemini_bridge
COPY README.md LICENSE ./

# 127.0.0.1 inside the container: the container is the trust boundary. If you
# need to reach it from outside, map the port and put auth in front of it.
ENV GEMINI_BRIDGE_HOST=127.0.0.1
ENV GEMINI_BRIDGE_PORT=8081

EXPOSE 8081

# Basic container has no curl/wget by default; healthcheck via python.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8081/health', timeout=3).status==200 else 1)"

# Default to the HTTP API. Override the command for the MCP face:
#   docker run ... gemini-bridge mcp --transport stdio
CMD ["gemini-bridge", "serve", "--host", "0.0.0.0"]
