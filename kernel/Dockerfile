FROM python:3.12-slim
RUN addgroup --system app && adduser --system --ingroup app app
RUN mkdir -p /data/state /data/logs /data/dna && chown -R app:app /data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY kernel.py dna_parser.py .
USER app
EXPOSE 8001
CMD ["uvicorn", "kernel:app", "--host", "0.0.0.0", "--port", "8001"]
