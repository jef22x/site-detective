#!/usr/bin/env sh
# Provision the staging store (spec 10.4). Run after: docker compose up -d
# Installs WordPress + WooCommerce, sample product, COD gateway, SMTP -> Mailpit.
set -e

WP="docker compose exec -T wpcli wp"
URL="${STORE_URL:-http://localhost:8080}"
ADMIN_USER="${ADMIN_USER:-admin}"
ADMIN_PASS="${ADMIN_PASS:-changeme}"

echo "Waiting for WordPress files..."
until $WP core is-installed 2>/dev/null || $WP core version 2>/dev/null; do sleep 3; done

if ! $WP core is-installed 2>/dev/null; then
  $WP core install --url="$URL" --title="AutoQA Staging" \
    --admin_user="$ADMIN_USER" --admin_password="$ADMIN_PASS" \
    --admin_email="admin@staging.local" --skip-email
fi

$WP option update permalink_structure "/%postname%/"
$WP plugin install woocommerce --activate
$WP plugin install wp-mail-smtp --activate

# Point outgoing mail at Mailpit's SMTP.
$WP option patch update wp_mail_smtp mail '{"mailer":"smtp","from_email":"store@staging.local","from_name":"AutoQA Staging"}' --format=json || true
$WP option patch update wp_mail_smtp smtp '{"host":"mailpit","port":1025,"encryption":"none","auth":false,"autotls":false}' --format=json || true

# Sample product + Cash on Delivery gateway.
$WP wc product create --name="Mock Hoodie" --type=simple --regular_price=25 \
  --sku=AUTOQA-HOODIE --user="$ADMIN_USER" || true
$WP option update woocommerce_cod_settings '{"enabled":"yes","title":"Cash on delivery"}' --format=json

echo "Done. Store: $URL  wp-admin: $URL/wp-admin ($ADMIN_USER/$ADMIN_PASS)  Mailpit: http://localhost:8025"
