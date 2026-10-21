FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends git openssh-client ca-certificates && rm -rf /var/lib/apt/lists/* && useradd --create-home --uid 10001 gitmesh && mkdir /data && chown gitmesh /data
WORKDIR /app
COPY gitmesh.py /app/gitmesh.py
COPY static /app/static
USER gitmesh
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "gitmesh.py"]
