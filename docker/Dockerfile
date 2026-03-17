FROM python:3.13-slim-bookworm

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY workers ./workers

RUN pip install --no-cache-dir .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--app-dir", "src"]
