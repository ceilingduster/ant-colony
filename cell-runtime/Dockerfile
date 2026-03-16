FROM python:3.12-slim
RUN addgroup --system app && adduser --system --ingroup app app
RUN mkdir -p /data/workspace /data/memory /data/logs /data/dna && chown -R app:app /data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY cell.py .
USER app
EXPOSE 8004
CMD ["uvicorn", "cell:app", "--host", "0.0.0.0", "--port", "8004"]
