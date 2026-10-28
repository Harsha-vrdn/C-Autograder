# Linux toolchain image for the AutoGrader (Ubuntu 24.04, gcc 13 with ASan/UBSan/LSan).
# The project folder is bind-mounted at /app (see docker-compose.yml), so the database, reports,
# uploads and the LLM cache stay on the host and grades match the host's.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libc6-dev python3 python3-venv ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN python3 -m venv /venv && /venv/bin/pip install -q -r /tmp/requirements.txt

WORKDIR /app
EXPOSE 8000
CMD ["sh", "-c", "/venv/bin/python manage.py migrate -v0 && /venv/bin/python manage.py runserver 0.0.0.0:8000"]
