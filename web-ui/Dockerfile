FROM python:3.12-slim
RUN addgroup --system app && adduser --system --ingroup app app
RUN mkdir -p /data/logs /etc/traefik/dynamic && chown -R app:app /data /etc/traefik/dynamic
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY static/ static/
USER app
EXPOSE 8006
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8006", "--log-level", "info"]
