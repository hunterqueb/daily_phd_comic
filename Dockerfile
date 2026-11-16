FROM python:3.13-alpine

# Install the time zone data. The service uses it for the day change.
RUN apk add --no-cache tzdata

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PHD_PORT=8080

WORKDIR /app
COPY phd_daily.py .

USER nobody
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"

CMD ["python", "phd_daily.py"]
