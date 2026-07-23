FROM python:3.12-alpine
RUN apk add --no-cache rclone
WORKDIR /app
COPY simplebot.py .
CMD ["python", "-u", "simplebot.py"]
