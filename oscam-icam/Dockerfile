FROM alpine:3.20

RUN apk add --no-cache \
    libusb \
    pcsc-lite-libs \
    ca-certificates

WORKDIR /oscam

COPY oscam /usr/bin/oscam
RUN chmod +x /usr/bin/oscam

VOLUME /config

EXPOSE 8888
EXPOSE 9000

ENTRYPOINT ["/usr/bin/oscam", "-c", "/config"]
