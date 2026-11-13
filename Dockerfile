FROM python:3.11-slim-bookworm@sha256:b1add8a6f2aca6bcfcf0b9c9b522352f7ce0d62a3d556a2f2f32511aa0cca250

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

COPY requirements.txt start.py LICENSE ./
COPY src ./src

RUN pip install --no-deps --require-hashes -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/

EXPOSE 5999

CMD ["python3", "-m", "src.start"]
