# Two-stage build: compile Vite app, then serve static dist/ with Caddy.
#
# VITE_* env vars are read by Vite at BUILD time and inlined into the JS
# bundle as string constants. Railway passes service variables down as
# Docker build args automatically when a matching ARG is declared, so
# rotating any of these requires a redeploy (not just a variable update).

FROM node:20-alpine AS build
WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY . .

ARG VITE_SUPABASE_URL
ARG VITE_SUPABASE_ANON_KEY
ARG VITE_PASSWORD_HASH
ENV VITE_SUPABASE_URL=$VITE_SUPABASE_URL
ENV VITE_SUPABASE_ANON_KEY=$VITE_SUPABASE_ANON_KEY
ENV VITE_PASSWORD_HASH=$VITE_PASSWORD_HASH

RUN npm run build


FROM caddy:2-alpine
COPY --from=build /app/dist /srv
COPY Caddyfile /etc/caddy/Caddyfile

# Caddy reads $PORT from Railway; default to 80 for local docker run.
EXPOSE 80
