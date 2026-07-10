FROM python:3.13-slim

COPY requirements.txt /temp/requirements.txt

RUN pip install --no-cache-dir -r /temp/requirements.txt

COPY ./src /src

# Tells Docker where to run commands from
WORKDIR /src

# Recommended way to run the app
CMD ["python", "app.py"]