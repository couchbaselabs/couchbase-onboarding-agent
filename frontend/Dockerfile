# Vite bakes VITE_API_BASE_URL/VITE_WS_BASE_URL into the static JS bundle at BUILD
# time -- they have to be passed as build args (see docker-compose.yml), and a
# plain `docker compose up` without `--build` will not pick up a change to them.
ARG VITE_API_BASE_URL=http://localhost:8000
ARG VITE_WS_BASE_URL=ws://localhost:8000

FROM node:20-slim AS build
WORKDIR /app

COPY certs/ /usr/local/share/ca-certificates/extra/
RUN if find /usr/local/share/ca-certificates/extra -name '*.crt' 2>/dev/null | grep -q .; then \
      apt-get update && \
      apt-get install -y --no-install-recommends ca-certificates && \
      update-ca-certificates && \
      rm -rf /var/lib/apt/lists/*; \
      export NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt; \
    fi

COPY package.json package-lock.json* ./
RUN npm install

COPY . .
ARG VITE_API_BASE_URL
ARG VITE_WS_BASE_URL
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}
ENV VITE_WS_BASE_URL=${VITE_WS_BASE_URL}
RUN npm run build

FROM node:20-slim AS serve
WORKDIR /app
RUN npm install -g serve@14
COPY --from=build /app/dist ./dist
EXPOSE 5173
CMD ["serve", "-s", "dist", "-l", "5173"]
