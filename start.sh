#!/bin/bash

echo "--- Starting Xewee Backend Setup Script ---"

# Remove existing virtual environment to ensure a clean slate
echo "1. Removing existing virtual environment..."
rm -rf /app/.venv

# Create a new virtual environment
echo "2. Creating a new virtual environment..."
python -m venv /app/.venv

# Activate the new virtual environment (though not strictly necessary for explicit calls below)
source /app/.venv/bin/activate

# Upgrade pip within the new virtual environment
echo "3. Upgrading pip..."
/app/.venv/bin/pip install --upgrade pip

# Aggressively uninstall old/conflicting python-telegram-bot packages
echo "4. Uninstalling old 'telegram' and 'python-telegram-bot' packages..."
/app/.venv/bin/pip uninstall -y telegram python-telegram-bot || true

# Force install the correct python-telegram-bot version
echo "5. Force installing python-telegram-bot==20.8..."
/app/.venv/bin/pip install --no-cache-dir --force-reinstall python-telegram-bot==20.8

# Install the rest of the dependencies from requirements.txt
echo "6. Installing other dependencies from requirements.txt..."
/app/.venv/bin/pip install --no-cache-dir -r requirements.txt

echo "--- Setup Complete. Starting Uvicorn Server ---"
# Start the Uvicorn server, using the pip from the new virtual environment
exec /app/.venv/bin/uvicorn main:app --host 0.0.0.0 --port $PORT