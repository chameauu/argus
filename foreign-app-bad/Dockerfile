FROM python:3.13-slim

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY requirements.txt .
RUN uv pip install --system --no-cache -r requirements.txt

COPY app.py .

RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8098

CMD ["gunicorn", "--bind", "0.0.0.0:8098", "--workers", "1", "--threads", "8", "--log-level", "info", "app:app"]
