FROM python:3.14-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

ENV PORT=8080
ENV SITES_DIR=/data/sites
EXPOSE 8080

USER 1000:1000

CMD ["python", "server.py"]
