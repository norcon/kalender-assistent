# Updater: Docker-CLI mit Compose-Plugin plus ein kleiner Python-Dienst.
FROM docker:29.9.2-cli
RUN apk add --no-cache python3
COPY updater.py /opt/updater/updater.py
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/status', timeout=3)"
ENTRYPOINT ["python3", "/opt/updater/updater.py"]
