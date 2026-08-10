FROM python:3.12-alpine
RUN apk add --no-cache rclone
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY VERSION .
COPY simplebot.py .
COPY islandbot ./islandbot
CMD ["python", "-u", "simplebot.py"]
