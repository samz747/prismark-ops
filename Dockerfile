FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
RUN useradd --create-home --uid 1001 app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY manage.py .
RUN mkdir -p /data/uploads && chown -R app:app /data /srv
USER app
EXPOSE 8000
# proxy headers are trusted only from the Caddy container on the private network
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*", "--workers", "2"]
