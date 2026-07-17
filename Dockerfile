FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY monitor.py .

# Railway inyecta variables de entorno automáticamente con esto
CMD ["sh", "-c", "python monitor.py"]
