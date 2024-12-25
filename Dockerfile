FROM python:3.12-rc-slim
WORKDIR /app/

# install binaries - ffmpeg (or avconv) needed by pydub (to be fast)
RUN apt-get update --fix-missing
RUN apt-get install ffmpeg gcc -y

# install dependencies
COPY ./requirements.txt /tmp/
RUN pip install -r /tmp/requirements.txt

WORKDIR /app/

# copy entrypoint files
COPY ./log_conf.json /app/
COPY ./entrypoint.sh /app/

# copy in config
COPY ./config/* /app/config/

# copy in app source
COPY ./src/*.py /app/src/

# copy in web source
COPY ./web /app/web/

# test application
COPY ./mypy.ini /app/
RUN mypy src --config-file mypy.ini

# run application
ENTRYPOINT ./entrypoint.sh
