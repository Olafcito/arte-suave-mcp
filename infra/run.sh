#!/bin/bash
# Lambda Web Adapter entrypoint: start the ASGI app with uvicorn.
# The adapter (AWS_LAMBDA_EXEC_WRAPPER=/opt/bootstrap) proxies the Function URL
# to http://localhost:$PORT. PYTHONPATH includes the deps/ dir in the zip.
export PYTHONPATH="$LAMBDA_TASK_ROOT:$LAMBDA_TASK_ROOT/deps:$PYTHONPATH"
exec python -m uvicorn arte_suave_mcp.server:app --host 0.0.0.0 --port "${PORT:-8080}"
