# x402 Research Brief server — Docker image (Render / Zeabur / any Docker PaaS).
# The server is stdlib-only Python; no dependencies to install.
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt x402-server.py ./

RUN pip install --no-cache-dir -r requirements.txt

# PaaS injects PORT; the server reads $PORT (default 8931 locally).
# Set X402_PAY_TO in the platform dashboard (public address, not a secret).
EXPOSE 8931

CMD ["python", "x402-server.py"]
