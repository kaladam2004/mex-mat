# Python image
FROM python:3.12-slim

# Пешгирии cache ва тезтар кор кардан
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Папкаи корӣ
WORKDIR /app

# System dependencies (барои баъзе package-ҳо)
RUN apt-get update && apt-get install -y build-essential

# Requirements copy
COPY requirements.txt .

# Install python packages
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy project
COPY . .

# Порт
EXPOSE 8000

# Run FastAPI
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]