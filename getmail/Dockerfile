FROM python:3.11-slim

RUN apt-get update && \
    apt-get install -y msmtp msmtp-mta msmtp-queue ca-certificates cron logrotate \
                       apache2 php libapache2-mod-php && \
    pip install getmail6 && \
    mkdir -p /var/log/getmail /var/spool/msmtp-queue

COPY crontab /etc/cron.d/getmail-cron
RUN chmod 0644 /etc/cron.d/getmail-cron && crontab /etc/cron.d/getmail-cron

COPY getmail-logrotate /etc/logrotate.d/getmail-logrotate

COPY web/ /var/www/html/

CMD service apache2 start && cron -f
