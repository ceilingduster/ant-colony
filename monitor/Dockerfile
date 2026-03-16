FROM python:3.12-slim
RUN addgroup --system app && adduser --system --ingroup app app
RUN mkdir -p /data/logs && chown -R app:app /data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY monitor.py .
USER app
EXPOSE 8005
CMD ["uvicorn", "monitor:app", "--host", "0.0.0.0", "--port", "8005"]
