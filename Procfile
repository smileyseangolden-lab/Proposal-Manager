web: gunicorn --bind 0.0.0.0:$PORT --worker-class gthread --workers ${WEB_WORKERS:-2} --threads ${WEB_THREADS:-8} --timeout 600 --graceful-timeout 30 --keep-alive 75 app:app
