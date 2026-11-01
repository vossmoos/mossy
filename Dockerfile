FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
  && apt-get install -y --no-install-recommends git \
  && rm -rf /var/lib/apt/lists/*

# requirements.lock pins exact versions, so the image matches what was tested.
# docker build --build-arg MOSSY_REQUIREMENTS="-r requirements.txt -r requirements-teams.txt" .
ARG MOSSY_REQUIREMENTS="-r requirements.txt"
COPY requirements.txt requirements-teams.txt requirements.lock ./
RUN pip install --no-cache-dir -c requirements.lock $MOSSY_REQUIREMENTS

COPY . .

ENV PORT=8000
ENV PLATFORMER_HOST=0.0.0.0

EXPOSE 8000

CMD ["python", "main.py", "--no-cli", "--host", "0.0.0.0", "--port", "8000"]
