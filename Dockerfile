FROM python:3.14-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends git openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && echo 'app:x:1000:1000:app:/home/app:/usr/sbin/nologin' >> /etc/passwd \
    && install -d -o 1000 -g 1000 /home/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

ENV PORT=8080
ENV SITES_DIR=/data/sites
EXPOSE 8080

USER 1000:1000

CMD ["python", "server.py"]
