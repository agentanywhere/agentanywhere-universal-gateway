#!/bin/bash
# Generate self-signed SSL certificate for development/testing
# For production, use certificates from Let's Encrypt or your CA

set -e

SSL_DIR="./nginx/ssl"
DAYS_VALID=365

echo "🔐 Generating SSL certificate for development/testing..."

# Create SSL directory
mkdir -p "$SSL_DIR"

# Generate private key
openssl genrsa -out "$SSL_DIR/key.pem" 2048

# Generate certificate signing request.
# Git Bash on Windows rewrites an argument that starts with "/" into a Windows
# path, which turns the subject below into "C:/Program Files/Git/C=US/..." and
# makes openssl fail. MSYS_NO_PATHCONV (Git for Windows) and
# MSYS2_ARG_CONV_EXCL (MSYS2) switch that off for this one command; every
# other shell ignores both variables.
MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' \
  openssl req -new -key "$SSL_DIR/key.pem" -out "$SSL_DIR/csr.pem" \
  -subj "/C=US/ST=State/L=City/O=Organization/CN=localhost"

# Generate self-signed certificate
openssl x509 -req -days $DAYS_VALID -in "$SSL_DIR/csr.pem" -signkey "$SSL_DIR/key.pem" \
  -out "$SSL_DIR/cert.pem"

# Set permissions
chmod 600 "$SSL_DIR/key.pem"
chmod 644 "$SSL_DIR/cert.pem"

# Clean up CSR
rm "$SSL_DIR/csr.pem"

echo "✅ SSL certificate generated:"
echo "   Certificate: $SSL_DIR/cert.pem"
echo "   Private Key: $SSL_DIR/key.pem"
echo ""
echo "⚠️  WARNING: This is a self-signed certificate for development only!"
echo "   For production, use certificates from Let's Encrypt or your CA."
echo ""
echo "📝 To use Let's Encrypt in production:"
echo "   1. Install certbot: apt-get install certbot python3-certbot-nginx"
echo "   2. Run: certbot --nginx -d yourdomain.com"
echo "   3. Certificates will be auto-renewed"


