FROM python:3.12

WORKDIR /app

RUN pip install uv

# Copy requirements first for better caching
COPY server/requirements.txt .
RUN uv pip install --system -r requirements.txt

# Install mem0 in editable mode
WORKDIR /app/packages
COPY pyproject.toml .
COPY README.md .
COPY mem0 ./mem0
RUN uv pip install --system -e .[graph]

# Return to app directory and copy server code
WORKDIR /app

# AWS credentials: copy from read-only mount to writable location
RUN mkdir -p /root/.aws
ENV AWS_SHARED_CREDENTIALS_FILE=/root/.aws/credentials
ENV AWS_CONFIG_FILE=/root/.aws/config

COPY server .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
