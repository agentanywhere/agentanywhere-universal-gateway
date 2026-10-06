FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY a2a_server.py .

EXPOSE 8002

CMD ["python", "a2a_server.py"]

