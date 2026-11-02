FROM node:lts-alpine AS builder
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci
COPY . .
ARG VITE_API_URL
ARG VITE_GIPHY_API_KEY
ENV VITE_API_URL=$VITE_API_URL
ENV VITE_GIPHY_API_KEY=$VITE_GIPHY_API_KEY
RUN npm run build

FROM nginx:stable-alpine
COPY --from=builder /app/dist /usr/share/nginx/html
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
