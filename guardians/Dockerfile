FROM python:3.12-slim
RUN addgroup --system app && adduser --system --ingroup app app
RUN mkdir -p /data/workspace /data/memory /data/logs /data/dna /etc/traefik/dynamic && chown -R app:app /data /etc/traefik/dynamic
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY queen.py antking.py wiseoldant.py nurse.py app.py .
USER app
EXPOSE 8002
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8002"]
