FROM ghcr.io/moli-xia/linux-autobook:latest

# Small deployment overlay for the gateway/worker protocol and on-demand PDG
# routing. The large application image remains the published upstream base.
COPY autobook_linux/config.py /opt/autobook-linux/autobook_linux/config.py
COPY autobook_linux/gateway_client.py /opt/autobook-linux/autobook_linux/gateway_client.py
COPY autobook_linux/gateway_server.py /opt/autobook-linux/autobook_linux/gateway_server.py
COPY autobook_linux/pdg_crypto.py /opt/autobook-linux/autobook_linux/pdg_crypto.py
COPY autobook_linux/pdg_fallback.py /opt/autobook-linux/autobook_linux/pdg_fallback.py
COPY autobook_linux/pipeline.py /opt/autobook-linux/autobook_linux/pipeline.py
COPY autobook_linux/panel/schema.py /opt/autobook-linux/autobook_linux/panel/schema.py
COPY docker/entrypoint.sh /usr/local/bin/autobook-entrypoint

RUN chmod +x /usr/local/bin/autobook-entrypoint \
 && /opt/autobook-linux/.venv/bin/python -m compileall -q /opt/autobook-linux/autobook_linux
