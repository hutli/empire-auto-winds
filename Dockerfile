FROM python:3.12-rc-slim
WORKDIR /app/

# install binaries - ffmpeg (or avconv) needed by pydub (to be fast)
RUN apt-get update
RUN apt-get install ffmpeg -y

# install dependencies
COPY ./requirements.txt /tmp/
RUN pip install -r /tmp/requirements.txt

WORKDIR /app/

# copy in config
COPY ./config/* /app/config/

# copy in app source
COPY ./src/*.py /app/src/

RUN mypy src --config-file /app/config/mypy.ini

CMD python ./src/main.py