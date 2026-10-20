FROM nginx:alpine

# Copy nginx configuration. nginx.conf holds upstream/server blocks only, so it
# goes where the stock /etc/nginx/nginx.conf includes it (inside http {}).
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Create SSL directory
RUN mkdir -p /etc/nginx/ssl

# Create log directory
RUN mkdir -p /var/log/nginx

# Expose ports
EXPOSE 80 443

# Health check
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD wget --no-verbose --tries=1 --spider http://localhost/health || exit 1

CMD ["nginx", "-g", "daemon off;"]


