FROM python:3.12-slim

WORKDIR /app

# Prefer IPv4 over IPv6 in getaddrinfo. This host's Docker bridge is IPv4-only
# (no ip6tables MASQUERADE), so any IPv6 connection attempt fails with
# ENETUNREACH / EADDRNOTAVAIL and the Anthropic SDK surfaces it as
# "Connection error." Elevating the v4-mapped-v4 precedence above the default
# IPv6 precedence makes glibc return A records first so httpx connects over v4.
RUN printf '\nprecedence ::ffff:0:0/96  100\n' >> /etc/gai.conf

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Create runtime directories and run as a non-root user (defense in depth: a
# code-exec bug in gunicorn no longer runs as uid 0). If host directories are
# bind-mounted (see docker-compose.yml) they must be writable by uid 10001.
RUN mkdir -p uploads generated_proposals data \
    && useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 5000

# Threaded workers (gthread) so a long inline AI call doesn't block other
# requests; gthread heartbeats independently so long streaming requests still
# complete within --timeout. Tune with WEB_WORKERS / WEB_THREADS.
# Binds to $PORT when the platform injects one and falls back to 5000.
CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT:-5000} --worker-class gthread --workers ${WEB_WORKERS:-2} --threads ${WEB_THREADS:-8} --timeout 600 --graceful-timeout 30 --keep-alive 75 app:app"]
