FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends git openssh-client ca-certificates && rm -rf /var/lib/apt/lists/* && useradd --create-home --uid 10001 gitmesh && mkdir /data && chown gitmesh /data
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY gitmesh.py management.py inspection.py runtime.py transport.py version_report.py askpass.py /app/
RUN python -c "from pathlib import Path; p=Path('/app/askpass.py'); p.write_bytes(p.read_bytes().replace(b'\r\n', b'\n'))" && chmod 755 /app/askpass.py
COPY static /app/static
USER gitmesh
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "gitmesh.py"]
