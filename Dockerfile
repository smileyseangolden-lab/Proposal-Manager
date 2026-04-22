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

# Create runtime directories
RUN mkdir -p uploads generated_proposals

EXPOSE 5000

# Proposal generation with Claude Opus on a big RFP can legitimately take
# 5-10 minutes of streaming. Set gunicorn --timeout to 900s (15 min) so
# long-running jobs finish cleanly. The host nginx proxy_read_timeout
# MUST be >= this value or nginx will cut the connection first.
# --graceful-timeout lets an in-flight request finish during worker reload.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", \
     "--timeout", "900", "--graceful-timeout", "900", \
     "--keep-alive", "75", "app:app"]
