#!/bin/bash
# Disable all stdout/stderr before the server starts
exec uvicorn main:app --host 0.0.0.0 --port $PORT --log-level critical > /dev/null 2>&1
