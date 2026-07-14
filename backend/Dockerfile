FROM python:3.12-slim

ARG INSTALL_AGENTS=false

WORKDIR /app
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir uv
RUN if [ "$INSTALL_AGENTS" = "true" ]; then \
      apt-get update && apt-get install -y --no-install-recommends git && \
      uv pip install --system --no-cache '.[market,agents]' && \
      apt-get purge -y git && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*; \
    else \
      uv pip install --system --no-cache '.[market]'; \
    fi

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
