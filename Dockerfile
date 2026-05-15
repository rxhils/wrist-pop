# Hugging Face Spaces compatible Dockerfile.
# Port 7860 mandatory for HF Spaces. Single uvicorn process.
# Uses minimal cron requirements (no Ollama, no crewai — cloud LLM only).
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Slim deps + FastAPI server libs
COPY requirements_cron.txt .
RUN pip install --no-cache-dir -r requirements_cron.txt \
    && pip install --no-cache-dir "fastapi==0.115.4" "uvicorn[standard]==0.32.0" "sse-starlette==2.1.3"

# Copy app
COPY . .

# Pre-create outputs + manual_renders + images + videos directories
RUN mkdir -p /app/outputs /app/outputs/manual_renders /app/outputs/images /app/outputs/videos

# HF Spaces requires non-root user (UID 1000)
RUN useradd -m -u 1000 user && chown -R user:user /app
USER user

EXPOSE 7860

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "7860"]
