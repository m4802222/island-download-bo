FROM python:3.12-alpine
WORKDIR /app
COPY simplebot.py .
CMD ["python", "-u", "simplebot.py"]
