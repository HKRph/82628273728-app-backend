web: bash -c " \
  rm -rf /app/.venv && \
  python -m venv /app/.venv && \
  /app/.venv/bin/pip install --upgrade pip && \
  /app/.venv/bin/pip uninstall -y telegram python-telegram-bot || true && \
  /app/.venv/bin/pip install --no-cache-dir --force-reinstall python-telegram-bot==20.8 && \
  /app/.venv/bin/pip install --no-cache-dir -r requirements.txt && \
  /app/.venv/bin/uvicorn main:app --host 0.0.0.0 --port $PORT \
"
