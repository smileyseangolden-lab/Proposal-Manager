FROM python:3.12-slim

WORKDIR /app

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
