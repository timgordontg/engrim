FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1

COPY . /app
RUN pip install --no-cache-dir .

ENTRYPOINT ["engrim", "serve", "--mcp"]
