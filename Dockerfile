FROM python:3.12-alpine
WORKDIR /app
COPY bosun.py .
CMD ["python", "-u", "bosun.py"]
