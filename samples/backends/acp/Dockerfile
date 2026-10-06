FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY acp_server.py .

EXPOSE 8005

CMD ["python", "acp_server.py"]
