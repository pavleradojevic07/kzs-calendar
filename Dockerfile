FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy
WORKDIR /app
COPY . .
RUN pip install -r requirements.txt
CMD gunicorn -b 0.0.0.0:${PORT:-8000} --timeout 180 app:app
