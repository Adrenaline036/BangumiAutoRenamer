FROM swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io/library/python:3.10-slim-bullseye@sha256:c78143fd1cf31073f538a255d2e8a365c75e3f595d97e1a7d09b722cd83973bd

ARG BAR_BUILD_ID=development
ARG BAR_SOURCE_FINGERPRINT=unverified
ARG BAR_APP_VERSION=unverified

RUN test "$BAR_BUILD_ID" != "development" \
    && test "$BAR_SOURCE_FINGERPRINT" != "unverified" \
    && test "$BAR_APP_VERSION" != "unverified"

LABEL io.bangumi-auto-renamer.build-id="${BAR_BUILD_ID}" \
      io.bangumi-auto-renamer.source-fingerprint="${BAR_SOURCE_FINGERPRINT}" \
      io.bangumi-auto-renamer.version="${BAR_APP_VERSION}"

ENV TZ=Asia/Shanghai
ENV BAR_BUILD_ID=${BAR_BUILD_ID}
ENV BAR_SOURCE_FINGERPRINT=${BAR_SOURCE_FINGERPRINT}
ENV BAR_APP_VERSION=${BAR_APP_VERSION}

WORKDIR /Bangumi_Auto_Rename

COPY . /Bangumi_Auto_Rename

RUN echo "deb https://mirrors.aliyun.com/debian/ bullseye main non-free contrib" > /etc/apt/sources.list
RUN apt-get update && apt-get install -y git && apt-get install -y python-is-python3
RUN apt-get install -y libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libatspi2.0-0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libxkbcommon0 libasound2 libpango-1.0-0 libcairo2
RUN pip install --no-deps --require-hashes -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/

EXPOSE 5999

CMD ["sh", "-c", "pwd && ls /Bangumi_Auto_Rename && python3 -m src.start"]
