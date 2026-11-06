FROM python:3.14-slim-trixie

WORKDIR /app

COPY requirements.txt .
# Drop pip afterwards: it isn't needed at runtime and its vendored copies of
# urllib3/msgpack/setuptools are what CVE scanners flag.
RUN pip install --no-cache-dir -r requirements.txt \
 && pip uninstall -y pip

COPY volvo_client.py VERSION ./

RUN mkdir -p /app/data
VOLUME ["/app/data"]

ENV PORT=8080
EXPOSE 8080

# Exactly one worker: the refresh token rotates on every use, so two worker
# processes would invalidate each other's token. Threads handle concurrency.
ENV LOG_LEVEL=INFO PYTHONUNBUFFERED=1
CMD ["gunicorn", "-b", "0.0.0.0:8080", "-w", "1", "--threads", "4", "--timeout", "30", "--log-level", "info", "volvo_client:app"]
