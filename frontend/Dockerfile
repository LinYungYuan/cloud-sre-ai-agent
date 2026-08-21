FROM node:24.19.0-alpine AS build

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.29.4-alpine

COPY --chown=101:101 nginx.conf /etc/nginx/nginx.conf
COPY --from=build --chown=101:101 /app/dist/frontend/browser/ /usr/share/nginx/html/

EXPOSE 8080

# These paths must be mounted writable when the root filesystem is read-only.
VOLUME ["/tmp", "/var/cache/nginx", "/var/run"]

USER 101
