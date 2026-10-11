# syntax=docker/dockerfile:1
FROM python:3.14-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8490
CMD ["uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8490", "--proxy-headers"]
