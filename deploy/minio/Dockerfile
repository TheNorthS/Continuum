FROM python:3.12-slim
ARG TARGETARCH
COPY deploy/minio/download.py /tmp/download.py
RUN python /tmp/download.py "$TARGETARCH" && useradd --uid 10002 --create-home minio && mkdir /data && chown minio:minio /data
USER minio
EXPOSE 9000 9001
ENTRYPOINT ["/usr/local/bin/minio"]
CMD ["server", "/data", "--console-address", ":9001"]
