FROM python:3.14.9-slim-trixie@sha256:35caba83af2fe6da9a5d922f9b0156f21d1246a48278c58a08bd253303ba42e8

WORKDIR /app

RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends git openssh-client \
    && dpkg --compare-versions "$(dpkg-query -W -f='${Version}' perl-base)" ge '5.40.1-6+deb13u1' \
    && rm -rf /var/lib/apt/lists/* \
    && echo 'app:x:1000:1000:app:/home/app:/usr/sbin/nologin' >> /etc/passwd \
    && install -d -o 1000 -g 1000 /home/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && pip check

COPY server.py .

ENV PORT=8080
ENV SITES_DIR=/data/sites
EXPOSE 8080

USER 1000:1000

CMD ["python", "server.py"]
