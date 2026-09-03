FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy by glob rather than enumerating modules: an explicit list silently
# drops any new module and only fails at runtime, which is how nyt.py was
# missed once already.
COPY *.py /app/
COPY providers.json /app/providers.json

VOLUME ["/data"]
EXPOSE 8781

# One worker: the renewal loop lives in a background thread and must not run
# in more than one process, or providers would be renewed concurrently.
CMD ["gunicorn", "--bind", "0.0.0.0:8781", "--workers", "1", "--threads", "4", "--timeout", "180", "app:app"]
