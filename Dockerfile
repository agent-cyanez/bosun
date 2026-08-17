FROM python:3.12-alpine
WORKDIR /app
COPY bosun.py .
HEALTHCHECK --interval=60s --timeout=5s CMD pgrep -f bosun.py || exit 1
CMD ["python", "-u", "bosun.py"]
