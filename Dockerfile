FROM node:22-alpine AS frontend
WORKDIR /build
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 API_HOST=0.0.0.0
COPY pyproject.toml README.md ./
COPY backend/ ./backend/
RUN pip install --no-cache-dir .
COPY migrations/ ./migrations/
COPY alembic.ini ./
COPY --from=frontend /build/dist ./frontend/dist
RUN mkdir -p /app/data && useradd --uid 10001 --create-home app && chown -R app:app /app
USER app
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "trading.app:app", "--host", "0.0.0.0", "--port", "8000"]
