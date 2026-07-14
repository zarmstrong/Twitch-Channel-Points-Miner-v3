FROM python:3.14-bookworm

WORKDIR /usr/src/app

COPY ./requirements.txt ./

ENV PYTHONUNBUFFERED=1

RUN pip install --upgrade pip

RUN apt-get update
RUN apt-get upgrade -y
RUN DEBIAN_FRONTEND=noninteractive apt-get install -qq -y --fix-missing --no-install-recommends \
    gcc \
    zlib1g-dev \
    libblas-dev \
    liblapack-dev \
    make \
    cmake \
    automake \
    ninja-build \
    g++ \
    subversion \
  && pip install -r requirements.txt \
  && pip cache purge \
  && apt-get remove -y gcc \
  && apt-get autoremove -y \
  && apt-get autoclean -y \
  && apt-get clean -y \
  && rm -rf /var/lib/apt/lists/* \
  && rm -rf /usr/share/doc/*

ADD ./TwitchChannelPointsMiner ./TwitchChannelPointsMiner
ADD ./twitchdrops_app_scraper.py ./twitchdrops_app_scraper.py
ADD ./assets ./assets
ADD ./example.py ./example.py
ADD ./config.example.py ./config.example.py
ENTRYPOINT [ "python", "-u", "-m", "TwitchChannelPointsMiner.runner" ]
