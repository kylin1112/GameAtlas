FROM node:22.20.0-bookworm-slim
RUN npm install --global --prefix /opt/node --no-audit --no-fund @earendil-works/pi-coding-agent@0.84.2 \
    && cp /usr/local/bin/node /opt/node/bin/node \
    && PATH=/opt/node/bin:$PATH /opt/node/bin/pi --version
# Export /opt/node into a new named volume; task images must match its architecture.
CMD ["sh", "-c", "cp -a /opt/node/. /runtime/"]
