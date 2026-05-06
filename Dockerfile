FROM python:3.12-slim

# Install the server and its deps from this repo's source. Glama (and
# anyone else who wants a containerized MCP server) gets a working
# stdio server out of the box. Customer-side configuration is a single
# env var: SPARKIT_API_KEY. The container starts without one — the MCP
# initialize handshake works without auth — but any actual research()
# call will fail until SPARKIT_API_KEY is provided at runtime.
WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir .

ENTRYPOINT ["sparkit-mcp"]
